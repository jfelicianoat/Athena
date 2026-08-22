"""Que la memoria de proyecto sea algo más que un almacén con buenas intenciones.

`project_memory.py` traía tres reglas escritas y bien argumentadas: una conclusión del
modelo no es un hecho, todo lleva procedencia y fecha, y corregir es superponer y no borrar.
Todas ciertas, y ninguna se ejercitaba, porque **nada escribía en la memoria**. Athena la
leía en cada run, la encontraba vacía y volvía a descubrir los mismos comandos.

Lo que se prueba aquí es lo que hace que exista de verdad: que se aprenda de la evidencia y
no de lo que el modelo creyó, que lo viejo se note, y que el escalón más alto siga siendo
inalcanzable sin una persona.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from athena.adapters.service.orchestration import OrchestrationSettings, Orchestrator
from athena.errors import AthenaRuntimeError
from athena.events import InMemoryEventBus
from athena.project_memory import (
    STALE_AFTER,
    MemoryKind,
    SqliteProjectMemory,
    VerificationState,
    render_for_context,
)
from athena.session_store import SqliteSessionStore
from athena.stores import InMemoryToolResultStore
from athena.testing import FakeModelProvider
from athena.verification import VerificationEvidence, VerificationResult, VerificationStatus


def _memoria(tmp_path: Path) -> SqliteProjectMemory:
    return SqliteProjectMemory(tmp_path / "memory.db")


def _resultado(*checks: tuple[str, str, bool]) -> VerificationResult:
    return VerificationResult(
        VerificationStatus.PASSED,
        tuple(
            VerificationEvidence(
                kind="test",
                summary=f"{name}: {'passed' if passed else 'failed'}",
                metadata={"name": name, "command": command, "passed": passed},
            )
            for name, command, passed in checks
        ),
        "listo",
    )


# -- aprender de la evidencia ------------------------------------------------


def _orquestador(tmp_path: Path) -> tuple[Orchestrator, SqliteProjectMemory | None]:
    ajustes = OrchestrationSettings(memory=_memoria(tmp_path))
    return Orchestrator(
        FakeModelProvider([]),
        InMemoryEventBus(),
        SqliteSessionStore(tmp_path / "sessions.db"),
        InMemoryToolResultStore(),
        ajustes,
    ), ajustes.memory


def test_solo_se_aprende_de_lo_que_se_ejecuto_y_paso(tmp_path: Path) -> None:
    """Un check que falló no es un comando que funcione.

    Guardarlo igual convertiría la memoria en una lista de cosas que probar, y la próxima
    sesión empezaría repitiendo el error de ésta con la confianza de un recuerdo.
    """

    async def scenario() -> None:
        orquestador, memoria = _orquestador(tmp_path)
        assert memoria is not None

        aprendidos = await orquestador.learn_from(
            "proyecto-1",
            _resultado(
                ("tests", "pytest -q", True),
                ("lint", "ruff check .", False),
            ),
            "run-1",
        )

        assert aprendidos == 1
        guardados = await memoria.active("proyecto-1")
        assert [item.content for item in guardados] == ["pytest -q"]

    asyncio.run(scenario())


def test_lo_que_el_runtime_ejecuto_entra_ya_verificado(tmp_path: Path) -> None:
    """No es un ascenso de cortesía: el comando corrió aquí y salió bien.

    Es la única promoción que Athena puede hacerse a sí misma con fundamento. La
    siguiente —que alguien responda por ello— tiene que venir de una persona.
    """

    async def scenario() -> None:
        orquestador, memoria = _orquestador(tmp_path)
        assert memoria is not None
        await orquestador.learn_from("proyecto-1", _resultado(("tests", "pytest -q", True)), "r1")

        (item,) = await memoria.active("proyecto-1")

        assert item.verification_state is VerificationState.VERIFIED, (
            "el runtime ejecuto ese comando y salio bien: eso es que algo lo comprobo"
        )
        assert item.source == "run:r1", "un recuerdo sin procedencia no se puede juzgar"

    asyncio.run(scenario())


def test_sin_verificacion_no_hay_nada_que_aprender(tmp_path: Path) -> None:
    """Un run que no pudo comprobar nada tampoco descubrió nada que guardar."""

    async def scenario() -> None:
        orquestador, memoria = _orquestador(tmp_path)
        assert memoria is not None

        assert await orquestador.learn_from("proyecto-1", None, "r1") == 0
        assert await memoria.active("proyecto-1") == ()

    asyncio.run(scenario())


def test_aprender_no_puede_tumbar_un_run_que_ya_termino(tmp_path: Path) -> None:
    """Lo que se pierde al fallar aquí es un recuerdo. Lo que se salvaría, nada."""

    class MemoriaRota:
        async def propose(self, *args: object, **kwargs: object) -> object:
            raise AthenaRuntimeError("el disco se lleno")

    async def scenario() -> None:
        orquestador = Orchestrator(
            FakeModelProvider([]),
            InMemoryEventBus(),
            SqliteSessionStore(tmp_path / "sessions.db"),
            InMemoryToolResultStore(),
            OrchestrationSettings(memory=MemoriaRota()),  # type: ignore[arg-type]
        )

        assert await orquestador.learn_from("p", _resultado(("t", "pytest -q", True)), "r") == 0

    asyncio.run(scenario())


# -- caducidad ---------------------------------------------------------------


def test_cada_tipo_de_recuerdo_envejece_a_su_ritmo(tmp_path: Path) -> None:
    """Un único plazo tendría que ser el del más volátil o el del más estable.

    El primero tiraría lo que sí dura; el segundo conservaría mentiras. Un comando de hace
    tres meses probablemente ya no exista; una decisión de arquitectura de hace un año
    sigue siendo cierta.
    """

    async def scenario() -> None:
        memoria = _memoria(tmp_path)
        comando = await memoria.propose(
            "p", MemoryKind.VERIFIED_COMMAND, "pytest -q", source="run:1"
        )
        decision = await memoria.propose(
            "p", MemoryKind.ARCHITECTURE_DECISION, "sin dependencias", source="run:1"
        )
        dentro_de_dos_meses = datetime.now(UTC) + timedelta(days=60)

        assert comando.is_stale(now=dentro_de_dos_meses)
        assert not decision.is_stale(now=dentro_de_dos_meses)
        assert STALE_AFTER["verified_command"] < STALE_AFTER["architecture_decision"]

    asyncio.run(scenario())


def test_lo_viejo_se_dice_en_vez_de_esconderse(tmp_path: Path) -> None:
    """Viejo no es falso, así que no se tira: se etiqueta.

    Ocultarlo perdería una pista que a menudo sigue valiendo. Darlo sin fecha lo
    presentaría como si valiese seguro, que es la única forma en que esta memoria puede
    hacer daño.
    """

    async def scenario() -> None:
        memoria = _memoria(tmp_path)
        item = await memoria.propose("p", MemoryKind.VERIFIED_COMMAND, "make test", source="run:1")

        fresco = render_for_context([item])
        viejo = render_for_context([item], now=datetime.now(UTC) + timedelta(days=400))

        assert "make test" in fresco and "stale" not in fresco
        assert "make test" in viejo, "se tiró una pista que podía seguir sirviendo"
        assert "stale" in viejo

    asyncio.run(scenario())


def test_una_memoria_vacia_no_gasta_ni_una_linea_del_prompt() -> None:
    assert render_for_context([]) == ""


# -- el escalón que sólo sube una persona ------------------------------------


def test_athena_no_puede_ascender_un_recuerdo_a_confirmado_por_el_usuario() -> None:
    """Nada del runtime nombra `USER_CONFIRMED` salvo donde lo pide una persona.

    Si algo lo hiciera, el escalón dejaría de significar lo que dice: «una persona
    respondió por esto». Athena llega a `VERIFIED` porque ejecuta cosas; no puede llegar
    más arriba, porque no es una persona.

    Los dos sitios exentos son el módulo que define el estado y el endpoint HTTP, que es
    exactamente el sitio por donde entra la persona.
    """
    import athena

    raiz = Path(athena.__file__).parent
    culpables = [
        str(fichero.relative_to(raiz))
        for fichero in raiz.rglob("*.py")
        if fichero.name not in ("project_memory.py", "server.py")
        and "USER_CONFIRMED" in fichero.read_text(encoding="utf-8")
    ]

    assert culpables == [], (
        f"algo del runtime asciende recuerdos a confirmados por el usuario: {culpables}"
    )


def test_un_recuerdo_no_puede_bajar_de_categoria(tmp_path: Path) -> None:
    """Degradar dejaría un item que pareció fiable sin rastro de por qué dejó de serlo."""

    async def scenario() -> None:
        memoria = _memoria(tmp_path)
        item = await memoria.propose("p", MemoryKind.DOMAIN_FACT, "algo", source="run:1")
        await memoria.approve(item.id, state=VerificationState.USER_CONFIRMED)

        with pytest.raises(AthenaRuntimeError):
            await memoria.approve(item.id, state=VerificationState.PROPOSED)

    asyncio.run(scenario())


def test_corregir_es_superponer_y_no_borrar(tmp_path: Path) -> None:
    """«Antes creíamos X» es justo lo que necesita quien depura una decisión equivocada."""

    async def scenario() -> None:
        memoria = _memoria(tmp_path)
        viejo = await memoria.propose("p", MemoryKind.VERIFIED_COMMAND, "make test", source="run:1")

        nuevo = await memoria.update(viejo.id, "pytest -q", source="run:2")

        assert nuevo.supersedes == viejo.id
        anterior = await memoria.get(viejo.id)
        assert anterior is not None, "se borró lo que se creía antes"
        activos = [item.content for item in await memoria.active("p")]
        assert activos == ["pytest -q"]

    asyncio.run(scenario())
