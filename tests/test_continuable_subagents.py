"""Volver a preguntarle al mismo delegado sin que eso sea un delegado nuevo.

ADR-010 daba por hecho que un subagente contesta una vez. Es barato de razonar y caro de
usar: el padre recibe un informe, tiene una pregunta obvia sobre él, y la única salida era
delegar otra vez desde cero — pagando de nuevo todo lo que el hijo ya había averiguado.

Lo que estas pruebas defienden es el límite, no la comodidad. «Continuable» sin tope es una
factura sin tope con otro nombre, y un delegado que renueva su presupuesto en cada pregunta
es «tantos agentes como quieras, contados como uno».
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from athena.cancellation import CancellationSource
from athena.errors import BudgetExceededError, ToolValidationError
from athena.events import EventName, InMemoryEventBus, RuntimeEvent
from athena.git_tools import git_read_tools
from athena.models import ModelResponse, ModelToolCall
from athena.permissions import DenyingPermissionPrompt
from athena.repository_tools import repository_read_tools
from athena.stores import InMemoryToolResultStore
from athena.subagent_provider import Continuable, Delegator
from athena.subagents import (
    SubagentBrief,
    SubagentBudget,
    SubagentRole,
    SubagentRunner,
)
from athena.testing import FakeModelProvider
from athena.tools import Tool
from athena.workspace import Workspace

RESPUESTA = (
    '{"relevant_files": ["a.py"], "findings": ["hay un bug"], "risks": [], '
    '"recommended_next_steps": []}'
)


def _runner(
    respuestas: list[ModelResponse], bus: InMemoryEventBus
) -> tuple[SubagentRunner, list[RuntimeEvent]]:
    eventos: list[RuntimeEvent] = []
    bus.subscribe(eventos.append)
    runner = SubagentRunner(
        FakeModelProvider(respuestas),
        _catalogo(),
        bus,
        InMemoryToolResultStore(),
        prompt=DenyingPermissionPrompt(),
    )
    return runner, eventos


def _catalogo() -> dict[str, Tool]:
    """Lo que el perfil del explorer exige, que incluye git de lectura."""
    return {tool.spec.name: tool for tool in (*repository_read_tools(), *git_read_tools())}


def _sandbox(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "a.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    return root


# -- identidad ---------------------------------------------------------------


def test_un_seguimiento_es_el_mismo_delegado_y_no_uno_nuevo(tmp_path: Path) -> None:
    """Conserva su id, así que todo lo que publique se le sigue atribuyendo a él.

    Con un nombre nuevo el registro enseñaría dos agentes donde hubo uno, y el presupuesto
    compartido no cuadraría con nada de lo que se ve.
    """
    root = _sandbox(tmp_path / "repo")

    async def scenario() -> None:
        bus = InMemoryEventBus()
        runner, eventos = _runner(
            [ModelResponse(RESPUESTA, "scripted", "stop") for _ in range(2)], bus
        )
        workspace = Workspace.from_path(root)
        token = CancellationSource().token

        primero = await runner.delegate(
            SubagentRole.EXPLORER,
            SubagentBrief(objective="Mira a.py"),
            workspace,
            token,
            parent_session_id="padre",
        )
        segundo = await runner.follow_up(
            primero.session_id,
            "¿Y qué pasa con las pruebas?",
            workspace,
            token,
            parent_session_id="padre",
        )

        assert segundo.session_id == primero.session_id
        continuados = [e for e in eventos if e.name is EventName.SUBAGENT_CONTINUED]
        assert len(continuados) == 1
        assert continuados[0].payload["follow_up"] == 1
        assert continuados[0].correlation_id == primero.session_id

    asyncio.run(scenario())


def test_el_delegado_recibe_lo_que_ya_habia_averiguado(tmp_path: Path) -> None:
    """Es lo que hace que continuar salga más barato que empezar otro de cero."""
    root = _sandbox(tmp_path / "repo")

    async def scenario() -> None:
        bus = InMemoryEventBus()
        proveedor = FakeModelProvider(
            [ModelResponse(RESPUESTA, "scripted", "stop") for _ in range(2)]
        )
        runner = SubagentRunner(
            proveedor,
            _catalogo(),
            bus,
            InMemoryToolResultStore(),
            prompt=DenyingPermissionPrompt(),
        )
        workspace = Workspace.from_path(root)
        token = CancellationSource().token

        primero = await runner.delegate(
            SubagentRole.EXPLORER, SubagentBrief(objective="Mira a.py"), workspace, token
        )
        await runner.follow_up(primero.session_id, "¿Y las pruebas?", workspace, token)

        segunda_peticion = proveedor.requests[-1]
        prompt = "\n".join(message.content or "" for message in segunda_peticion.messages)
        assert "hay un bug" in prompt, "el delegado empezó de cero, sin lo que ya sabía"
        assert "¿Y las pruebas?" in prompt

    asyncio.run(scenario())


# -- límites -----------------------------------------------------------------


def test_el_presupuesto_es_del_delegado_y_no_de_cada_pregunta(tmp_path: Path) -> None:
    """Renovarlo en cada vuelta sería saltarse el límite sin llegar a tocarlo."""
    root = _sandbox(tmp_path / "repo")

    async def scenario() -> None:
        bus = InMemoryEventBus()
        runner, _ = _runner(
            [
                ModelResponse(
                    "",
                    "scripted",
                    "tool_calls",
                    tool_calls=(ModelToolCall("t1", "glob", {"pattern": "*.py"}),),
                ),
                ModelResponse(RESPUESTA, "scripted", "stop"),
                ModelResponse(RESPUESTA, "scripted", "stop"),
            ],
            bus,
        )
        workspace = Workspace.from_path(root)
        token = CancellationSource().token

        primero = await runner.delegate(
            SubagentRole.EXPLORER,
            SubagentBrief(objective="Mira a.py"),
            workspace,
            token,
            budget=SubagentBudget(max_tool_calls=10, max_follow_ups=2),
        )
        sesion = runner.session(primero.session_id)
        gastado = sesion.tool_calls_spent
        assert gastado >= 1, "la primera vuelta gastó herramientas"

        await runner.follow_up(primero.session_id, "¿Y las pruebas?", workspace, token)

        assert sesion.remaining().max_tool_calls < 10, (
            "el seguimiento recibió un presupuesto entero en vez del que quedaba"
        )

    asyncio.run(scenario())


def test_no_se_puede_preguntar_indefinidamente(tmp_path: Path) -> None:
    """El tope existe porque cada vuelta cuesta llamadas al modelo."""
    root = _sandbox(tmp_path / "repo")

    async def scenario() -> None:
        bus = InMemoryEventBus()
        runner, _ = _runner([ModelResponse(RESPUESTA, "scripted", "stop") for _ in range(4)], bus)
        workspace = Workspace.from_path(root)
        token = CancellationSource().token

        primero = await runner.delegate(
            SubagentRole.EXPLORER,
            SubagentBrief(objective="Mira a.py"),
            workspace,
            token,
            budget=SubagentBudget(max_follow_ups=1),
        )
        await runner.follow_up(primero.session_id, "una", workspace, token)

        with pytest.raises(BudgetExceededError):
            await runner.follow_up(primero.session_id, "dos", workspace, token)

    asyncio.run(scenario())


def test_un_delegado_de_un_solo_uso_no_queda_registrado(tmp_path: Path) -> None:
    """Sin seguimientos concedidos no hay sesión que guardar.

    Guardarla igualmente dejaría a cada delegación de un run ocupando memoria por si
    acaso, que es una fuga que sólo se nota en los runs largos.
    """
    root = _sandbox(tmp_path / "repo")

    async def scenario() -> None:
        bus = InMemoryEventBus()
        runner, _ = _runner([ModelResponse(RESPUESTA, "scripted", "stop")], bus)

        resultado = await runner.delegate(
            SubagentRole.EXPLORER,
            SubagentBrief(objective="Mira a.py"),
            Workspace.from_path(root),
            CancellationSource().token,
            budget=SubagentBudget(max_follow_ups=0),
        )

        with pytest.raises(ToolValidationError):
            runner.session(resultado.session_id)

    asyncio.run(scenario())


def test_un_delegado_cerrado_no_contesta_mas(tmp_path: Path) -> None:
    root = _sandbox(tmp_path / "repo")

    async def scenario() -> None:
        bus = InMemoryEventBus()
        runner, _ = _runner([ModelResponse(RESPUESTA, "scripted", "stop") for _ in range(2)], bus)
        workspace = Workspace.from_path(root)
        token = CancellationSource().token

        primero = await runner.delegate(
            SubagentRole.EXPLORER,
            SubagentBrief(objective="Mira a.py"),
            workspace,
            token,
            budget=SubagentBudget(max_follow_ups=2),
        )
        runner.close(primero.session_id)

        with pytest.raises(ToolValidationError):
            await runner.follow_up(primero.session_id, "otra cosa", workspace, token)

    asyncio.run(scenario())


def test_preguntarle_a_un_delegado_que_no_existe_no_crea_uno(tmp_path: Path) -> None:
    """Crearlo silenciosamente daría un agente nuevo con la etiqueta de otro."""
    root = _sandbox(tmp_path / "repo")

    async def scenario() -> None:
        bus = InMemoryEventBus()
        runner, _ = _runner([ModelResponse(RESPUESTA, "scripted", "stop")], bus)

        with pytest.raises(ToolValidationError):
            await runner.follow_up(
                "no-existe",
                "algo",
                Workspace.from_path(root),
                CancellationSource().token,
            )

    asyncio.run(scenario())


def test_una_pregunta_vacia_no_es_una_pregunta(tmp_path: Path) -> None:
    root = _sandbox(tmp_path / "repo")

    async def scenario() -> None:
        bus = InMemoryEventBus()
        runner, _ = _runner([ModelResponse(RESPUESTA, "scripted", "stop") for _ in range(2)], bus)
        workspace = Workspace.from_path(root)
        token = CancellationSource().token
        primero = await runner.delegate(
            SubagentRole.EXPLORER,
            SubagentBrief(objective="Mira a.py"),
            workspace,
            token,
            budget=SubagentBudget(max_follow_ups=2),
        )

        with pytest.raises(ToolValidationError):
            await runner.follow_up(primero.session_id, "   ", workspace, token)

    asyncio.run(scenario())


# -- quién puede continuar ---------------------------------------------------


def test_solo_el_explorer_es_continuable_por_defecto() -> None:
    """Un coder continuable sería una edición sin criterio de aceptación nuevo.

    Y eso es exactamente lo que ADR-015 no quiere: que pida otra delegación, con su
    criterio, en vez de seguir tocando lo que ya tocó.
    """
    from athena.subagents import DEFAULT_PROFILES

    assert DEFAULT_PROFILES[SubagentRole.EXPLORER].budget.max_follow_ups > 0
    assert DEFAULT_PROFILES[SubagentRole.CODER].budget.max_follow_ups == 0
    assert DEFAULT_PROFILES[SubagentRole.VERIFIER].budget.max_follow_ups == 0


def test_continuar_es_una_capacidad_aparte_de_delegar() -> None:
    """No todo delegador puede recordar a un hijo entre llamadas.

    Meterlo en el Protocol principal obligaría a todos a declarar que saben hacerlo, y el
    que no supiera fallaría al ejecutarlo en vez de al declararse.
    """

    class SoloDelega:
        async def delegate(self, *args: object, **kwargs: object) -> None: ...

    assert not isinstance(SoloDelega(), Continuable)
    assert isinstance(SoloDelega(), Delegator)


def test_un_seguimiento_no_puede_cambiar_el_rol_por_la_puerta_de_atras() -> None:
    """Pedirle el rol otra vez invitaría a cambiárselo, que es un escalado indirecto."""
    from athena.delegation import parse_delegation

    peticion = parse_delegation(
        {"goal": "¿y las pruebas?", "follow_up_to": "hijo-1", "role": "coder"}
    )

    assert peticion.is_follow_up
    assert peticion.follow_up_to == "hijo-1"
    assert peticion.goal == "¿y las pruebas?"


# -- que llegue vivo hasta el modelo ------------------------------------------


def test_el_modelo_puede_pedir_un_seguimiento_por_su_nombre(tmp_path: Path) -> None:
    """La costura entera, de la tool al runner pasando por el servicio de proveedores.

    Es la comprobacion que este proyecto necesita mas que ninguna otra: el subsistema
    existia, estaba probado y llegaba a `SubagentService`, que no sabia continuar — asi
    que ningun run real habria podido usarlo nunca.
    """
    from athena.delegation import DelegateTaskTool
    from athena.permissions import PermissionPolicy
    from athena.subagent_provider import (
        NativeAthenaSubagentProvider,
        SubagentProviderRegistry,
        SubagentService,
    )
    from athena.tools import ToolContext

    root = _sandbox(tmp_path / "repo")

    async def scenario() -> None:
        bus = InMemoryEventBus()
        runner = SubagentRunner(
            FakeModelProvider([ModelResponse(RESPUESTA, "scripted", "stop") for _ in range(2)]),
            _catalogo(),
            bus,
            InMemoryToolResultStore(),
            prompt=DenyingPermissionPrompt(),
        )
        servicio = SubagentService(
            SubagentProviderRegistry((NativeAthenaSubagentProvider(runner),))
        )
        tool = DelegateTaskTool(servicio, _catalogo(), PermissionPolicy())
        contexto = ToolContext("padre", Workspace.from_path(root), "c1")
        token = CancellationSource().token

        primero = await tool.execute(
            contexto,
            {
                "goal": "Mira a.py",
                "role": "explorer",
                "acceptance_criteria": ["decir que hay"],
            },
            token,
        )
        salida = primero.output
        assert isinstance(salida, dict)
        hijo = salida["delegate_session_id"]
        assert isinstance(hijo, str) and hijo
        assert salida["follow_ups_left"] == 2, (
            "el modelo tiene que ver cuantas le quedan: un limite que no se ve se "
            "descubre chocando con el"
        )

        segundo = await tool.execute(
            contexto, {"goal": "¿y las pruebas?", "follow_up_to": hijo}, token
        )
        continuado = segundo.output
        assert isinstance(continuado, dict)
        assert continuado["delegate_session_id"] == hijo
        assert continuado["follow_ups_left"] == 1

    asyncio.run(scenario())


def test_un_despliegue_que_no_sabe_continuar_lo_dice_en_vez_de_delegar_otra_vez(
    tmp_path: Path,
) -> None:
    """Delegar de nuevo en silencio daria un agente nuevo con la etiqueta de otro."""
    from athena.delegation import DelegateTaskTool
    from athena.permissions import PermissionPolicy
    from athena.tools import ToolContext

    root = _sandbox(tmp_path / "repo")

    class SoloDelega:
        async def delegate(self, *args: object, **kwargs: object) -> None:  # pragma: no cover
            raise AssertionError("no deberia llegar a delegar")

    async def scenario() -> None:
        tool = DelegateTaskTool(SoloDelega(), _catalogo(), PermissionPolicy())  # type: ignore[arg-type]

        with pytest.raises(ToolValidationError):
            await tool.execute(
                ToolContext("padre", Workspace.from_path(root), "c1"),
                {"goal": "¿y las pruebas?", "follow_up_to": "hijo-1"},
                CancellationSource().token,
            )

    asyncio.run(scenario())


def test_al_modelo_no_se_le_devuelve_vacio_una_delegacion_que_fue_bien() -> None:
    """El caso general elegia `files_changed` como «lo que se enumera».

    Para un explorer, que no cambia ficheros, eso es una lista vacia: al modelo se le
    contestaba «(sin resultados)» despues de una delegacion que habia traido hallazgos.
    Se vio en un run real, y es exactamente para esto para lo que existe la costura de
    proyeccion.
    """
    from athena.delegation import DelegateTaskTool
    from athena.tools import ToolResult

    vista = DelegateTaskTool.project(
        DelegateTaskTool.__new__(DelegateTaskTool),
        ToolResult(
            {
                "role": "explorer",
                "status": "completed",
                "summary": '{"findings": ["hay un bug en add()"]}',
                "files_changed": [],
                "delegate_session_id": "hijo-1",
                "follow_ups_left": 2,
            }
        ),
    )

    assert "hay un bug en add()" in vista.model.text
    assert "follow_up_to" in vista.model.text, "no se le dice como volver a preguntar"
    assert "hijo-1" in vista.model.text
    assert vista.display.facts["delegate_session_id"] == "hijo-1"


def test_no_se_le_ofrece_seguir_a_quien_ya_no_puede() -> None:
    """Ofrecerselo le haria gastar una llamada en descubrir que no."""
    from athena.delegation import DelegateTaskTool
    from athena.tools import ToolResult

    vista = DelegateTaskTool.project(
        DelegateTaskTool.__new__(DelegateTaskTool),
        ToolResult(
            {
                "role": "coder",
                "status": "completed",
                "summary": "cambiado",
                "files_changed": ["calc.py"],
                "delegate_session_id": "hijo-2",
                "follow_ups_left": 0,
            }
        ),
    )

    assert "follow_up_to" not in vista.model.text
    assert "calc.py" in vista.model.text
