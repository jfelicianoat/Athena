"""Athena fuera del código: el fixture no-desarrollador, que es la prueba real del núcleo.

El informe de auditoría lo dijo antes de escribir nada: «Phase 8's non-developer profile is
the real test of the core». Athena se escribió para repositorios de software y lo decía en
todas partes —el prompt, el descubrimiento de comandos, y desde ADR-027 el hecho de que un
dominio sin comandos ejecutables terminase siempre en «no se pudo verificar»—.

Estas pruebas trabajan sobre una carpeta de documentos: sin `pyproject.toml`, sin
`package.json`, sin git, sin shell y sin nada que ejecutar. Si el núcleo estuviera acoplado
a la ingeniería de software, aquí se vería.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from athena.agent_loop import AgentLoop, AgentLoopConfig, AgentRunStatus
from athena.cancellation import CancellationSource
from athena.context import ContextBuilder
from athena.errors import ToolValidationError
from athena.events import InMemoryEventBus
from athena.models import ModelResponse, ModelToolCall
from athena.mutation_tools import workspace_mutation_tools
from athena.permissions import PermissionPolicy, PolicyPermissionEngine
from athena.profiles import (
    DOCUMENTS,
    SOFTWARE_ENGINEERING,
    AthenaProfile,
    Evidence,
    ProfileRegistry,
)
from athena.registry import ToolRegistry
from athena.repository_tools import repository_read_tools
from athena.state import AgentState, AgentStatus, SessionState
from athena.stores import InMemoryToolResultStore
from athena.testing import FakeModelProvider
from athena.tool_executor import ToolExecutor
from athena.verification import ArtifactVerificationPolicy, VerificationStatus
from athena.workspace import Workspace

INFORME = "informe.md"


def _despacho(root: Path) -> Path:
    """Un sitio de trabajo que no es un repositorio de software."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "notas.md").write_text(
        "# Notas de la reunion\n\n- Subir el precio del plan basico\n- Revisar bajas\n",
        encoding="utf-8",
    )
    (root / "actas.md").write_text("# Actas\n\nSe acuerda revisar precios.\n", encoding="utf-8")
    return root


def _run_de_documentos(root: Path, respuestas: list[ModelResponse]) -> AgentLoop:
    bus = InMemoryEventBus()
    catalogo = {
        tool.spec.name: tool for tool in (*repository_read_tools(), *workspace_mutation_tools(bus))
    }
    registry = ToolRegistry(DOCUMENTS.catalog_from(catalogo).values())  # type: ignore[arg-type]
    return AgentLoop(
        FakeModelProvider(respuestas),
        registry,
        ToolExecutor(
            registry,
            PolicyPermissionEngine(PermissionPolicy(allow_workspace_writes=True)),
            InMemoryToolResultStore(),
            bus,
        ),
        ContextBuilder(Workspace.from_path(root), subject=DOCUMENTS.subject),
        bus,
        verification=ArtifactVerificationPolicy((INFORME,)),
        config=AgentLoopConfig(max_iterations=4, session_timeout_seconds=60.0),
    )


# -- el fixture obligatorio --------------------------------------------------


def test_athena_termina_un_encargo_que_no_es_de_software(tmp_path: Path) -> None:
    """Un run completo sobre documentos, con evidencia y sin ejecutar nada.

    Antes de esto era imposible: sin comandos que descubrir la verificación no podía
    concluir, así que el único final disponible en cualquier dominio sin tests era «no se
    pudo comprobar». Correcto como diagnóstico, y una forma educada de decir que Athena
    sólo servía para código.
    """
    root = _despacho(tmp_path / "despacho")

    async def scenario() -> None:
        loop = _run_de_documentos(
            root,
            [
                ModelResponse(
                    "",
                    "scripted",
                    "tool_calls",
                    tool_calls=(
                        ModelToolCall(
                            "c1",
                            "write_file",
                            {
                                "path": INFORME,
                                "content": "# Informe\n\nSe propone subir el plan basico.\n",
                            },
                        ),
                    ),
                ),
                ModelResponse("Informe redactado.", "scripted", "stop"),
            ],
        )

        resultado = await loop.run(
            "Redacta informe.md a partir de las notas.",
            Workspace.from_path(root),
            CancellationSource().token,
        )

        assert resultado.status is AgentRunStatus.COMPLETED, (
            f"un encargo no-software no pudo terminar: {resultado.error}"
        )
        assert (root / INFORME).read_text(encoding="utf-8").startswith("# Informe")

    asyncio.run(scenario())


def test_el_perfil_de_documentos_no_tiene_shell_ni_git(tmp_path: Path) -> None:
    """Y no por prudencia: es lo que prueba que el núcleo no los necesitaba.

    Un perfil no-desarrollador que se quedase `bash` no demostraría nada — seguiría
    pudiendo ejecutar tests, y el acoplamiento que venimos a desmentir seguiría ahí sin
    que nadie lo notara.
    """
    del tmp_path

    assert "bash" not in DOCUMENTS.tools
    assert not [name for name in DOCUMENTS.tools if name.startswith("git")]
    assert "write_file" in DOCUMENTS.tools, "sin escribir no se produce nada"


def test_lo_que_el_perfil_no_incluye_no_existe_para_el_run(tmp_path: Path) -> None:
    """Estructural antes que política, igual que `registry_for()` con los roles.

    Una tool que no está en el catálogo no es que se deniegue: no se puede ni nombrar, así
    que negarla no depende de que una política esté bien configurada.
    """
    bus = InMemoryEventBus()
    catalogo = {
        tool.spec.name: tool for tool in (*repository_read_tools(), *workspace_mutation_tools(bus))
    }
    registry = ToolRegistry(DOCUMENTS.catalog_from(catalogo).values())  # type: ignore[arg-type]

    assert "bash" not in registry.names()
    del tmp_path


# -- lo que un perfil promete ------------------------------------------------


def test_un_perfil_dice_lo_que_su_evidencia_no_demuestra() -> None:
    """Un perfil puede declarar evidencia más débil; lo que no puede es callárselo.

    Sin esta regla, «perfiles» sería el sitio donde se aprueban runs sin comprobarlos:
    bastaría con inventar un dominio cuya prueba de éxito es que el modelo diga que sí.
    """
    for perfil in (SOFTWARE_ENGINEERING, DOCUMENTS):
        assert perfil.proves.strip(), f"{perfil.name} no dice qué demuestra"

    assert "does not establish" in DOCUMENTS.proves, (
        "la evidencia por entregables prueba que algo se produjo, no que sea bueno, "
        "y eso tiene que estar escrito donde se lea"
    )


def test_un_perfil_sin_herramientas_no_se_puede_construir() -> None:
    with pytest.raises(ValueError):
        AthenaProfile(
            name="vacio",
            subject="nada",
            evidence=Evidence.PRODUCED_ARTIFACTS,
            proves="nada",
            tools=(),
        )


def test_un_perfil_que_pide_lo_que_no_hay_falla_en_vez_de_conformarse() -> None:
    """Conformarse lo convertiría en otro perfil sin decirlo.

    Y el mismo run se comportaría distinto según la máquina donde corriese, que es la
    clase de diferencia que nadie encuentra hasta que importa.
    """
    with pytest.raises(ToolValidationError) as fallo:
        SOFTWARE_ENGINEERING.catalog_from(
            {tool.spec.name: tool for tool in repository_read_tools()}
        )

    assert "bash" in str(fallo.value)


# -- el registro -------------------------------------------------------------


def test_un_perfil_desconocido_es_un_error_y_no_una_caida_al_de_por_defecto() -> None:
    """Quien pide `documents` y recibe el de software no se entera a tiempo.

    Se entera cuando Athena intenta ejecutar los tests de una carpeta de textos, que es
    tarde y en el sitio equivocado.
    """
    registro = ProfileRegistry()

    assert registro.get("documents") is DOCUMENTS
    assert registro.get(None) is registro.default
    assert registro.get("") is registro.default
    with pytest.raises(ToolValidationError):
        registro.get("astrologia")


def test_el_de_por_defecto_es_el_de_siempre() -> None:
    """Introducir perfiles no puede cambiar lo que hace un despliegue que no los pidió."""
    assert ProfileRegistry().default is SOFTWARE_ENGINEERING


def test_dos_perfiles_con_el_mismo_nombre_no_se_registran() -> None:
    with pytest.raises(ValueError):
        ProfileRegistry([DOCUMENTS, DOCUMENTS])


# -- la evidencia por entregables --------------------------------------------


def _estado(files: tuple[str, ...]) -> SessionState:
    """El estado tal y como lo recibe una politica de verificacion.

    Lleva `files_modified` porque a la verificacion se le cuenta lo que el run hizo, no
    solo la ultima frase que dijo el modelo — que es justo lo unico que no es evidencia.
    """
    return SessionState(
        session_id="s-1",
        workspace_id="w-1",
        agent=AgentState(status=AgentStatus.VERIFYING),
        attributes={"files_modified": list(files)},
    )


def test_un_entregable_que_no_se_produjo_no_pasa(tmp_path: Path) -> None:
    async def scenario() -> None:
        politica = ArtifactVerificationPolicy((INFORME,))
        resultado = await politica.verify(
            _estado(()),
            Workspace.from_path(tmp_path),
            CancellationSource().token,
        )

        assert resultado.status is VerificationStatus.FAILED
        assert INFORME in resultado.summary

    asyncio.run(scenario())


def test_un_entregable_vacio_no_cuenta_como_producido(tmp_path: Path) -> None:
    """Un fichero de cero bytes existe y no es un entregable.

    Es el atajo más barato para un verde falso: crear el fichero y no escribir nada.
    """
    (tmp_path / INFORME).write_text("", encoding="utf-8")

    async def scenario() -> None:
        politica = ArtifactVerificationPolicy((INFORME,))
        resultado = await politica.verify(
            _estado((INFORME,)),
            Workspace.from_path(tmp_path),
            CancellationSource().token,
        )

        assert resultado.status is VerificationStatus.FAILED

    asyncio.run(scenario())


def test_un_entregable_que_ya_estaba_no_lo_produjo_este_run(tmp_path: Path) -> None:
    """Encontrarse el trabajo hecho no es haberlo hecho.

    Sin esta comprobación, un run que no tocó nada pasaría por haber cumplido el encargo
    siempre que alguien hubiera escrito el fichero antes.
    """
    (tmp_path / INFORME).write_text("# Informe de otro\n", encoding="utf-8")

    async def scenario() -> None:
        politica = ArtifactVerificationPolicy((INFORME,))
        resultado = await politica.verify(
            _estado(()),
            Workspace.from_path(tmp_path),
            CancellationSource().token,
        )

        assert resultado.status is VerificationStatus.FAILED

    asyncio.run(scenario())


def test_sin_entregables_ni_escrituras_no_se_afirma_nada(tmp_path: Path) -> None:
    """Inconcluso, no fallido: no hay con qué demostrar ni una cosa ni la otra."""

    async def scenario() -> None:
        politica = ArtifactVerificationPolicy()
        resultado = await politica.verify(
            _estado(()),
            Workspace.from_path(tmp_path),
            CancellationSource().token,
        )

        assert resultado.status is VerificationStatus.INCONCLUSIVE

    asyncio.run(scenario())


def test_lo_que_pasa_dice_ademas_lo_que_no_ha_probado(tmp_path: Path) -> None:
    (tmp_path / INFORME).write_text("# Informe\n\nContenido.\n", encoding="utf-8")

    async def scenario() -> None:
        politica = ArtifactVerificationPolicy((INFORME,))
        resultado = await politica.verify(
            _estado((INFORME,)),
            Workspace.from_path(tmp_path),
            CancellationSource().token,
        )

        assert resultado.status is VerificationStatus.PASSED
        assert resultado.permits_completion
        assert "does not establish" in resultado.summary

    asyncio.run(scenario())


def test_un_entregable_fuera_del_workspace_no_cuenta(tmp_path: Path) -> None:
    """El workspace sigue siendo el límite, también para la evidencia."""

    async def scenario() -> None:
        politica = ArtifactVerificationPolicy(("../fuera.md",))
        resultado = await politica.verify(
            _estado(()),
            Workspace.from_path(tmp_path),
            CancellationSource().token,
        )

        assert resultado.status is VerificationStatus.FAILED

    asyncio.run(scenario())


# -- lo que el cliente puede exigir ------------------------------------------


def test_nombrar_los_entregables_endurece_la_comprobacion() -> None:
    """Sin entregables declarados se comprueba lo que el run dice haber escrito.

    Es mas debil a proposito y se reporta como tal, pero el camino fuerte tiene que ser
    alcanzable desde fuera: una politica que solo el codigo de pruebas puede activar es
    una politica que en produccion no existe.
    """
    from athena.adapters.service.runs import RunOptions

    opciones = RunOptions.from_json(
        {"profile": "documents", "deliverables": ["informe.md", "  resumen.md  "]}
    )

    assert opciones.deliverables == ("informe.md", "resumen.md")
    assert opciones.profile == "documents"


def test_unos_entregables_que_no_son_rutas_se_rechazan() -> None:
    from athena.adapters.service.runs import RunOptions

    with pytest.raises(ToolValidationError):
        RunOptions.from_json({"deliverables": [{"path": "informe.md"}]})
