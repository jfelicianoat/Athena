"""Los diez escenarios E2E del prompt maestro, o los trozos que faltaban de ellos.

**Origen: MASTER_PROMPT.** Esta lista sí viene del encargo, a diferencia de la de
`test_acceptance_deepseek.py`, que se derivó de lo construido y lo dice. La distinción
importa y por eso viven en ficheros distintos: los derivados cubren regresiones reales y
se conservan, pero no tienen la autoridad del encargo original.

Este fichero **no repite** lo que ya estaba probado. El catálogo completo —qué escenario
lo cubre qué test— está en `docs/ACCEPTANCE_SCENARIOS.md`; aquí sólo están los trozos que
al contrastar los diez originales con la suite resultaron no estar cubiertos por nada:

- E2E-01: la cadena entera de un run simple en un solo sitio, incluida la ausencia de grafo.
- E2E-03: validez estructural de un grafo de un nodo, separada de si conviene descomponer.
- E2E-05: que el padre **no** recibe el transcript del delegado que reutiliza.
- E2E-06: un dominio no-desarrollador de verdad, con el fixture del encargo.
- E2E-07: la matriz de capability/visibility/authority entera, fila por fila.
- E2E-08: la mitad que faltaba de la carrera entre canales — la recuperación.
- E2E-10: los cuatro puntos de caída, y qué no debe sobrevivir a un reinicio.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import pytest

from athena.adapters.service.approvals import ApprovalRegistry, PendingApproval
from athena.adapters.service.orchestration import ExecutionMode, OrchestrationSettings
from athena.adapters.service.runs import CapabilityMode, RunOptions, RunRegistry
from athena.cancellation import CancellationSource, CancellationToken
from athena.delegation import confine, narrow
from athena.errors import ToolValidationError
from athena.events import EventName, InMemoryEventBus, ModelEvent, RuntimeEvent
from athena.models import (
    ModelCapabilities,
    ModelHealth,
    ModelHealthStatus,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    ModelToolCall,
)
from athena.permissions import (
    PermissionDecision,
    PermissionPolicy,
    PermissionRequest,
    PolicyPermissionEngine,
    RiskLevel,
    RiskTier,
)
from athena.planning import PlanningLimits, TaskGraph, TaskNode
from athena.profiles import AthenaProfile, Evidence, ProfileRegistry
from athena.registry import ToolRegistry
from athena.session_store import SessionRecord, SqliteSessionStore
from athena.state import AgentStatus
from athena.stores import SqliteToolResultStore
from athena.subagents import DEFAULT_PROFILES, SubagentRole
from athena.tools import ToolContext, ToolLoadPolicy, ToolResult, ToolSpec
from athena.types import JSONObject
from athena.workspace import Workspace

WORKING = "def add(a, b):\n    return a + b\n"
TEST = "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"


# ------------------------------------------------------------------ andamiaje


def _git(root: Path, *arguments: str) -> None:
    completed = subprocess.run(
        ["git", "-C", str(root), *arguments], capture_output=True, check=False, text=True
    )
    if completed.returncode != 0:
        pytest.fail(f"git {' '.join(arguments)} failed: {completed.stderr}")


def _repository(root: Path) -> Workspace:
    """Un repositorio cuyos propios checks pasan, para que verificar pruebe algo."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "calc.py").write_text(WORKING, encoding="utf-8")
    (root / "test_calc.py").write_text(TEST, encoding="utf-8")
    command = f'"{sys.executable}" -m pytest -q'
    (root / "AGENTS.md").write_text(
        f"# Sandbox\n\n## Verification\n\n```\n{command}\n```\n", encoding="utf-8"
    )
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "athena@example.invalid")
    _git(root, "config", "user.name", "Athena Test")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "initial")
    return Workspace.from_path(root)


class _Scripted(ModelProvider):
    """Contesta al planificador por su esquema y a todo lo demás con lo que se le dé."""

    def __init__(self, plan: str = "", *, calls: Sequence[ModelToolCall] = ()) -> None:
        self.plan = plan
        self._calls = list(calls)
        self.prompts: list[str] = []
        self.planning_requests = 0

    async def complete(
        self, request: ModelRequest, cancellation: CancellationToken
    ) -> ModelResponse:
        cancellation.raise_if_cancelled()
        self.prompts.append("\n".join(message.content for message in request.messages))
        if request.response_schema is not None:
            self.planning_requests += 1
            return ModelResponse(self.plan, "scripted", "stop")
        if self._calls:
            return ModelResponse("", "scripted", "tool_use", tool_calls=(self._calls.pop(0),))
        return ModelResponse("hecho", "scripted", "stop")

    async def stream(
        self, request: ModelRequest, cancellation: CancellationToken
    ) -> AsyncIterator[ModelEvent]:
        del request, cancellation
        if False:  # pragma: no cover - nunca se usa; el contrato pide el método
            yield ModelEvent(EventName.MODEL_COMPLETED, "never")

    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities(False, True, True)

    async def health(self, cancellation: CancellationToken) -> ModelHealth:
        cancellation.raise_if_cancelled()
        return ModelHealth(ModelHealthStatus.HEALTHY)


class _Detenido(_Scripted):
    """Se queda dentro de la llamada al modelo hasta que alguien lo suelta.

    Sirve para tener un run vivo de verdad —no uno a punto de acabar— mientras se
    ejercita algo que solo tiene sentido sobre un run vivo.
    """

    def __init__(self, suelta: asyncio.Event) -> None:
        # La primera vuelta pide una lectura y contesta al instante: hace falta para que
        # el run llegue a persistirse, que es lo que `start()` espera antes de devolver
        # un id. Detenerse en la primera dejaria colgado al que lo lanza, no al run.
        super().__init__(calls=[ModelToolCall("c1", "glob", {"pattern": "*.py"})])
        self._suelta = suelta

    async def complete(
        self, request: ModelRequest, cancellation: CancellationToken
    ) -> ModelResponse:
        cancellation.raise_if_cancelled()
        if self.prompts:
            await asyncio.wait_for(self._suelta.wait(), timeout=60)
        return await super().complete(request, cancellation)


def _registry(
    tmp_path: Path,
    provider: ModelProvider,
    bus: InMemoryEventBus,
    *,
    planning: bool = True,
    profiles: ProfileRegistry | None = None,
) -> RunRegistry:
    return RunRegistry(
        provider,
        bus,
        SqliteSessionStore(tmp_path / "sessions.db"),
        SqliteToolResultStore(tmp_path / "results.db"),
        orchestration=OrchestrationSettings(planning=planning),
        profiles=profiles,
    )


async def _settle(registry: RunRegistry, run_id: str) -> None:
    """Esperar a que el run termine de verdad, sin dormir a ciegas."""
    tarea = registry.run(run_id).task
    if tarea is not None:
        await asyncio.wait_for(tarea, timeout=120)


# ------------------------------------------------------- E2E-01 · Simple Direct


def test_e2e_01_un_objetivo_simple_se_hace_de_una_pieza_y_sin_grafo(tmp_path: Path) -> None:
    """Origen: MASTER_PROMPT. AUTO -> DIRECT -> AgentLoop -> tools -> verificacion -> PASS.

    La mitad que no estaba escrita en ningun sitio es la ultima: **no se crea un
    TaskGraph inutil**. Comprobar solo que el run termina bien dejaria pasar una version
    que planifica, monta el ejecutor de grafos y luego ejecuta una sola tarea — el mismo
    resultado, pagando una llamada al modelo y un ejecutor entero por nada.
    """
    workspace = _repository(tmp_path / "repo")
    bus = InMemoryEventBus()
    hechos: list[RuntimeEvent] = []
    bus.subscribe(hechos.append)
    provider = _Scripted()

    async def escenario() -> None:
        registry = _registry(tmp_path, provider, bus, planning=True)
        # Ejecucion concedida de forma explicita: los checks del sandbox son la evidencia
        # de este escenario, y desde A01 nada del proyecto corre sin esa autoridad.
        run_id = await registry.start(
            "Di como esta implementada la suma",
            workspace,
            RunOptions(execution=CapabilityMode.ALLOW),
        )
        await _settle(registry, run_id)

        record = await registry.snapshot(run_id)
        assert record is not None
        assert record.status is AgentStatus.COMPLETED
        assert record.verification.get("status") == "passed"

        nombres = {evento.name for evento in hechos}
        # Ni grafo ni tareas: la decision fue no descomponer, y no descomponer se nota
        # en que el ejecutor de grafos no llego a existir.
        assert EventName.GRAPH_STARTED not in nombres
        assert EventName.TASK_STARTED not in nombres
        # Y consta por que, con el codigo estable y no solo con una frase.
        decidido = next(e for e in hechos if e.name is EventName.PLAN_DECIDED)
        assert decidido.payload["executed_as"] == "direct"
        assert decidido.payload["execution_mode"] == "auto"
        assert provider.planning_requests == 0, "planificar un objetivo simple ya es el coste"

        await registry.shutdown()

    asyncio.run(escenario())


# ------------------------------------------------- E2E-03 · Forced One-Node Graph


def test_e2e_03_un_grafo_de_un_nodo_es_valido_aunque_no_convenga() -> None:
    """Origen: MASTER_PROMPT. Validez estructural y conveniencia son cosas distintas.

    `TaskGraph.build` responde a «¿es esto un plan?» y `DecompositionPolicy` a «¿merece
    la pena?». Juntarlas seria un error caro en las dos direcciones: un grafo de un nodo
    rechazado por invalido impediria fijar el camino a quien lo pide explicitamente, y
    una politica que aceptase cualquier plan valido pagaria el ejecutor siempre.
    """
    grafo = TaskGraph.build(
        [
            TaskNode(
                id="T01",
                goal="Resumir lo que hace el modulo",
                expected_output="un parrafo",
                acceptance_criteria=("nombra el modulo",),
                suggested_role=SubagentRole.EXPLORER,
            )
        ],
        PlanningLimits(),
    )

    assert len(grafo.nodes) == 1
    # Y es ejecutable: la frontera inicial lo contiene, que es lo que el ejecutor mira.
    assert [nodo.id for nodo in grafo.ready()] == ["T01"]


def test_e2e_03_pedir_jerarquico_ejecuta_el_grafo_de_un_nodo(tmp_path: Path) -> None:
    """Origen: MASTER_PROMPT. Quien fija el camino lo obtiene, aunque la politica diria que no.

    Es el complemento del anterior en el servicio: `execution_mode=hierarchical` con un
    plan de una sola tarea **corre por el ejecutor de grafos**. Caer al bucle «porque da
    igual» convertiria una peticion explicita en una sugerencia, y quien necesita saber
    cual corrio se quedaria sin saberlo.
    """
    workspace = _repository(tmp_path / "repo")
    bus = InMemoryEventBus()
    hechos: list[RuntimeEvent] = []
    bus.subscribe(hechos.append)
    plan = json.dumps(
        {
            "tasks": [
                {
                    "id": "T01",
                    "goal": "describe como esta implementada la suma",
                    "expected_output": "la funcion nombrada",
                    "acceptance_criteria": ["nombra un fichero y una funcion"],
                    "suggested_role": "explorer",
                }
            ]
        }
    )

    async def escenario() -> None:
        registry = _registry(tmp_path, _Scripted(plan), bus, planning=True)
        run_id = await registry.start(
            "Describe la suma",
            workspace,
            RunOptions(execution_mode=ExecutionMode.HIERARCHICAL),
        )
        await _settle(registry, run_id)

        nombres = [evento.name for evento in hechos]
        assert EventName.GRAPH_STARTED in nombres, "se pidio el grafo y no se uso"
        assert EventName.TASK_STARTED in nombres
        decidido = next(e for e in hechos if e.name is EventName.PLAN_DECIDED)
        assert decidido.payload["executed_as"] == "hierarchical"

        await registry.shutdown()

    asyncio.run(escenario())


# ------------------------------------------------- E2E-05 · Continuable Explorer


def test_e2e_05_al_padre_no_le_llega_el_transcript_del_delegado(tmp_path: Path) -> None:
    """Origen: MASTER_PROMPT. Reutilizar al hijo sin transferirle al padre su conversacion.

    Lo que ya estaba probado era la mitad de arriba: que un seguimiento es el mismo
    delegado, con su presupuesto compartido. Faltaba la de abajo, que es la razon de ser
    de delegar: al padre le llega un **informe**, no lo que el hijo leyo ni lo que se
    dijeron por el camino. Si el transcript subiera, delegar dejaria de ahorrar contexto
    y solo añadiria latencia.
    """
    from athena.git_tools import git_read_tools
    from athena.permissions import DenyingPermissionPrompt
    from athena.repository_tools import repository_read_tools
    from athena.stores import InMemoryToolResultStore
    from athena.subagents import SubagentBrief, SubagentRunner
    from athena.testing import FakeModelProvider

    workspace = _repository(tmp_path / "repo")
    secreto = "PISTA-QUE-SOLO-VE-EL-HIJO"
    catalogo = {tool.spec.name: tool for tool in (*repository_read_tools(), *git_read_tools())}
    bus = InMemoryEventBus()
    eventos: list[RuntimeEvent] = []
    bus.subscribe(eventos.append)

    # El hijo mira el repositorio y luego contesta. Lo que mira es lo que **no** debe
    # subir; lo que contesta, si.
    respuestas = [
        ModelResponse(
            "",
            "scripted",
            "tool_use",
            tool_calls=(ModelToolCall("c1", "grep", {"query": "def add"}),),
        ),
        ModelResponse(
            json.dumps({"findings": ["El operador esta bien"], "summary": "Nada roto"}),
            "scripted",
            "stop",
        ),
        ModelResponse(
            json.dumps({"findings": ["Linea 2"], "summary": "Esta en calc.py:2"}),
            "scripted",
            "stop",
        ),
    ]
    runner = SubagentRunner(
        FakeModelProvider(respuestas),
        catalogo,
        bus,
        InMemoryToolResultStore(),
        prompt=DenyingPermissionPrompt(),
    )

    async def escenario() -> None:
        primero = await runner.delegate(
            SubagentRole.EXPLORER,
            SubagentBrief(objective=f"Encuentra el fallo. Contexto: {secreto}"),
            workspace,
            CancellationSource().token,
            parent_session_id="padre",
        )
        segundo = await runner.follow_up(
            primero.session_id,
            "Y en que linea",
            workspace,
            CancellationSource().token,
            parent_session_id="padre",
        )

        # El mismo delegado, no uno nuevo: si fueran dos, el presupuesto no seria uno.
        assert segundo.session_id == primero.session_id

        # Lo que el hijo hizo se publica bajo **su** sesion, no bajo la del padre. Es lo
        # que permite atribuirselo sin que aparezca como trabajo del run.
        del_hijo = [e for e in eventos if e.session_id == primero.session_id]
        assert any(e.name is EventName.TOOL_COMPLETED for e in del_hijo), (
            "el hijo no llego a usar ninguna tool"
        )

        # Y en el ambito del padre solo consta el ciclo de vida del delegado: cuando
        # empezo, cuando siguio y como acabo. Nada de lo que leyo ni de lo que penso.
        del_padre = [e for e in eventos if e.session_id == "padre"]
        assert {e.name for e in del_padre} <= {
            EventName.SUBAGENT_STARTED,
            EventName.SUBAGENT_CONTINUED,
            EventName.SUBAGENT_COMPLETED,
            EventName.SUBAGENT_FAILED,
            EventName.SUBAGENT_CANCELLED,
        }
        # Lo que el hijo averiguo llega al padre como **valor de vuelta**, que es un
        # informe, y no por el flujo de eventos. Si su respuesta apareciera tambien en el
        # ambito del padre, cualquiera que escuche el run estaria leyendo el trabajo del
        # hijo en bruto, que es justo lo que delegar evita.
        assert primero.answer is not None and "Nada roto" in primero.answer
        assert segundo.answer is not None and "calc.py:2" in segundo.answer
        assert not any("Nada roto" in json.dumps(e.payload) for e in del_padre), (
            "la respuesta del hijo se publico en el ambito del padre"
        )

    asyncio.run(escenario())


# ------------------------------------------------ E2E-06 · Non-Developer Profile

#: El fixture del encargo: un analista financiero. Ni git, ni codigo fuente, ni pytest.
#:
#: Se construye aqui y no se registra en `ProfileRegistry` por defecto: Athena no ofrece
#: hoy un perfil financiero como producto, y añadirlo para que la prueba pase seria
#: fabricar el sujeto de la prueba. Lo que este escenario demuestra es que **el nucleo no
#: necesita nada de desarrollador**, y para eso basta con que el perfil exista aqui.
FINANCIAL_ANALYST = AthenaProfile(
    name="financial_analyst",
    subject="a folder of financial statements and notes",
    evidence=Evidence.PRODUCED_ARTIFACTS,
    proves=(
        "The declared deliverables exist, are non-empty and were written by this run. "
        "It does not establish that the figures are right."
    ),
    tools=("glob", "grep", "read_file", "read_range", "list_directory", "write_file"),
    description="Analisis sobre documentos financieros: no hay suite que pase.",
)


def test_e2e_06_athena_trabaja_en_un_dominio_sin_nada_de_desarrollador(tmp_path: Path) -> None:
    """Origen: MASTER_PROMPT. El nucleo de Athena es independiente del dominio.

    La carpeta no es un repositorio: no hay git, ni pyproject, ni una sola linea de
    codigo. Si algo del nucleo diera por hecho cualquiera de las tres, este run no
    llegaria a terminar — y hasta aqui la unica prueba de independencia era un perfil de
    documentos generico, que es una version mas suave de la misma afirmacion.
    """
    carpeta = tmp_path / "finanzas"
    carpeta.mkdir()
    (carpeta / "resultados-2026-Q1.csv").write_text(
        "concepto,importe\ningresos,120000\ngastos,90000\n", encoding="utf-8"
    )
    (carpeta / "notas.md").write_text(
        "# Notas\n\nEl trimestre cierra con margen positivo.\n", encoding="utf-8"
    )
    workspace = Workspace.from_path(carpeta)

    bus = InMemoryEventBus()
    provider = _Scripted(
        calls=[
            ModelToolCall("c1", "glob", {"pattern": "*.csv"}),
            ModelToolCall(
                "c2",
                "write_file",
                {"path": "resumen.md", "content": "# Resumen\n\nMargen de 30.000.\n"},
            ),
        ]
    )
    perfiles = ProfileRegistry([FINANCIAL_ANALYST], default="financial_analyst")

    async def escenario() -> None:
        registry = _registry(tmp_path, provider, bus, planning=False, profiles=perfiles)
        run_id = await registry.start(
            "Resume el trimestre en resumen.md",
            workspace,
            RunOptions(
                writes=CapabilityMode.ALLOW,
                profile="financial_analyst",
                deliverables=("resumen.md",),
            ),
        )
        await _settle(registry, run_id)

        record = await registry.snapshot(run_id)
        assert record is not None
        assert record.status is AgentStatus.COMPLETED
        # Se verifico, y por artefactos: no hay checks que ejecutar en una carpeta de
        # documentos, y decir «no verificado» seria castigar al dominio por no ser codigo.
        assert record.verification.get("status") == "passed"
        assert (carpeta / "resumen.md").read_text(encoding="utf-8").strip()
        # Y nunca existio nada de desarrollador: el perfil filtra por estructura, asi que
        # `bash` y `git_commit` no es que se denieguen, es que no estan.
        assert not (carpeta / ".git").exists()
        prompt = "\n".join(provider.prompts)
        assert "bash" not in prompt and "git_commit" not in prompt

        await registry.shutdown()

    asyncio.run(escenario())


# --------------------------------- E2E-07 · Capability / Visibility / Authority


def _peticion(
    workspace: Workspace,
    herramienta: str,
    *,
    tier: RiskTier,
    solo_lectura: bool,
) -> PermissionRequest:
    return PermissionRequest(
        tool_name=herramienta,
        operation=herramienta,
        action="hacer algo",
        workspace=workspace,
        risk=RiskLevel.LOW if solo_lectura else RiskLevel.MEDIUM,
        tier=tier,
        is_read_only=solo_lectura,
        is_destructive=False,
    )


def _nombres_visibles(definiciones: tuple[JSONObject, ...]) -> set[str]:
    """Los nombres que el modelo ve este turno, sacados de los esquemas que se le mandan.

    Se lee con cuidado y no con dos indexaciones seguidas: `definitions()` devuelve JSON
    generico, y encadenar `["function"]["name"]` sobre el obliga a callar al comprobador
    de tipos justo donde podria avisar de un cambio de forma.
    """
    nombres: set[str] = set()
    for definicion in definiciones:
        funcion = definicion.get("function")
        if isinstance(funcion, dict):
            nombre = funcion.get("name")
            if isinstance(nombre, str):
                nombres.add(nombre)
    return nombres


class _Diferida:
    """Una tool que existe y no se ve hasta que alguien la busca.

    Hecha a mano y no tomada del catalogo porque ninguna de las tools del nucleo es
    diferida: la carga diferida es para catalogos externos (MCP), y este escenario
    necesita justamente la fila de la matriz donde existir y verse se separan.
    """

    def __init__(self, name: str, hint: str) -> None:
        self._spec = ToolSpec(
            name=name,
            description=f"Hace {name}",
            input_schema={"type": "object", "properties": {"texto": {"type": "string"}}},
            output_schema={"type": "object"},
            risk=RiskLevel.MEDIUM,
            max_result_size_chars=1_000,
            load_policy=ToolLoadPolicy.DEFERRED,
            search_hint=hint,
        )

    @property
    def spec(self) -> ToolSpec:
        return self._spec

    def validate(self, arguments: JSONObject) -> JSONObject:
        return arguments

    def permission(self, context: ToolContext, arguments: JSONObject) -> PermissionRequest:
        return _peticion(
            context.workspace,
            self._spec.name,
            tier=RiskTier.R1_WORKSPACE_WRITE,
            solo_lectura=False,
        )

    async def execute(
        self,
        context: ToolContext,
        arguments: JSONObject,
        cancellation: CancellationToken,
    ) -> ToolResult:
        # Nunca se ejecuta: este escenario mide quien la ve y quien la puede usar, no
        # que hace. Fingir un resultado seria dar por probado algo que no se prueba aqui.
        raise NotImplementedError

    def is_read_only(self, arguments: JSONObject) -> bool:
        return False

    def is_destructive(self, arguments: JSONObject) -> bool:
        return False

    def is_concurrency_safe(self, arguments: JSONObject) -> bool:
        return False


def test_e2e_07_capability_visibility_y_authority_son_tres_preguntas(tmp_path: Path) -> None:
    """Origen: MASTER_PROMPT. La matriz entera, fila por fila.

    Tres preguntas distintas que se confunden con facilidad:

    - **Capability**: existe la herramienta en este despliegue?
    - **Visibility**: la ve el modelo ahora mismo?
    - **Authority**: se le deja usarla?

    Confundirlas produce dos fallos simetricos y los dos malos: creer que ocultar algo lo
    protege, y creer que enseñarlo lo autoriza.
    """
    from athena.mutation_tools import workspace_mutation_tools
    from athena.permissions import ReadOnlyPermissionEngine
    from athena.repository_tools import repository_read_tools

    workspace = Workspace.from_path(tmp_path, "ws")
    lecturas = {tool.spec.name: tool for tool in repository_read_tools()}
    escrituras = {tool.spec.name: tool for tool in workspace_mutation_tools()}

    escritura = _peticion(
        workspace, "write_file", tier=RiskTier.R1_WORKSPACE_WRITE, solo_lectura=False
    )

    # -- NO / -- / -- -> UnsupportedCapability -----------------------------------------
    # No esta montada, asi que no hay nada que denegar. Un registro que la nombrase y
    # luego la denegase mentiria sobre que existe.
    solo_lecturas = ToolRegistry(lecturas.values())
    with pytest.raises(ToolValidationError):
        solo_lecturas.get("write_file")

    # -- YES / NO / YES -> no accesible directamente al modelo -------------------------
    # Montada, con autoridad de sobra, y aun asi el modelo no la puede nombrar: no esta
    # en los esquemas de este turno. Es visibilidad, no permiso.
    diferido = ToolRegistry([*lecturas.values(), *escrituras.values()])
    diferido.register(_Diferida("jira_issue", "jira ticket incidencia"))
    visibles = _nombres_visibles(diferido.definitions())
    assert "jira_issue" not in visibles
    assert "jira_issue" in diferido.names(), "seguir existiendo es otra cosa que verse"

    permisivo = PolicyPermissionEngine(
        PermissionPolicy(allow_workspace_writes=True, allow_local_execution=False)
    )

    # -- YES / YES / DENY -> bloqueado --------------------------------------------------
    # La autoridad de un explorer: verlo todo y no poder escribir nada. Verla no
    # autoriza; es el error simetrico del anterior.
    assert ReadOnlyPermissionEngine().decide(escritura) is PermissionDecision.DENY

    # -- YES / YES / ASK -> espera aprobacion -------------------------------------------
    # Ni permite ni deniega: la unica fila que no se resuelve mirando una politica, y por
    # eso la unica en la que el sistema tiene que quedarse esperando sin decidir.
    restrictivo = PolicyPermissionEngine(
        PermissionPolicy(allow_workspace_writes=False, allow_local_execution=False)
    )
    assert restrictivo.decide(escritura) is PermissionDecision.ASK

    # -- YES / YES / ALLOW -> ejecuta ---------------------------------------------------
    assert permisivo.decide(escritura) is PermissionDecision.ALLOW

    # -- YES / DEFERRED / ALLOW -> descubrir, autorizar, ejecutar ------------------------
    # Descubrirla la hace visible; usarla sigue pasando por el motor, que es la fila
    # anterior otra vez. Si descubrir concediera permiso, bastaria con buscar para
    # escalar y la carga diferida se convertiria en un agujero.
    encontradas = diferido.search("jira ticket")
    assert [tool.spec.name for tool in encontradas] == ["jira_issue"]
    revelados = _nombres_visibles(diferido.definitions({"jira_issue"}))
    assert "jira_issue" in revelados
    assert permisivo.decide(escritura) is PermissionDecision.ALLOW

    # -- YES / YES / padre ALLOW, hijo DENY -> hijo bloqueado ---------------------------
    # La autoridad de un hijo es la interseccion con la de su padre, escrita como
    # aritmetica y no comprobada como regla: no hay camino que la ensanche.
    padre = PermissionPolicy(allow_workspace_writes=True, allow_local_execution=False)
    hijo = narrow(padre, PermissionPolicy(allow_workspace_writes=False))
    assert not hijo.allow_workspace_writes

    # Y al reves: un padre de solo lectura no puede encargar un hijo que escriba, por
    # mucho que el perfil que se pida sea el del coder.
    solo_leer = PermissionPolicy(allow_workspace_writes=False, allow_local_execution=False)
    coder = confine(
        DEFAULT_PROFILES[SubagentRole.CODER],
        solo_leer,
        frozenset({*lecturas, *escrituras}),
    )
    assert not coder.policy.allow_workspace_writes


# ------------------------------------------------- E2E-08 · Multi-Channel Race


def test_e2e_08_tras_el_conflicto_se_puede_escribir_sobre_la_revision_nueva(
    tmp_path: Path,
) -> None:
    """Origen: MASTER_PROMPT. La carrera entre canales, incluida la recuperacion.

    Lo que ya estaba probado era el choque: quien escribe sobre una revision vieja recibe
    un conflicto con el objetivo actual. Faltaba lo que viene despues, que es lo que hace
    que el conflicto sea util en vez de un callejon: releer, decidir con el objetivo
    nuevo delante, y volver a escribir sobre **esa** revision sin que nada se corrompa.
    """
    # Una carpeta sin checks a proposito: lo que este escenario mide es la carrera por la
    # revision, y montar encima una verificacion real solo añadiria un proceso que el
    # test tendria que esperar por motivos que no son los suyos.
    carpeta = tmp_path / "notas"
    carpeta.mkdir()
    (carpeta / "notas.md").write_text("# Notas\n", encoding="utf-8")
    workspace = Workspace.from_path(carpeta)
    bus = InMemoryEventBus()

    async def escenario() -> None:
        # El run se queda parado dentro de la llamada al modelo mientras dura la carrera.
        # Es lo que hace la prueba determinista: un run que corre de verdad podria
        # terminar antes de la primera revision, y entonces lo que se estaria midiendo
        # seria quien llego antes y no lo que pasa cuando dos canales escriben.
        suelta = asyncio.Event()
        registry = _registry(tmp_path, _Detenido(suelta), bus, planning=False)
        run_id = await registry.start("Lo que pidio ChatyGPT", workspace)

        # Telegram revisa primero. El run va ahora por la revision 2.
        de_telegram = registry.revise_goal(run_id, "Lo que pidio Telegram", base_revision=1)
        assert de_telegram.revision == 2

        # ChatyGPT llega con la 1 y choca. Nada se pisa y nada se fusiona.
        from athena.errors import GoalConflict

        with pytest.raises(GoalConflict) as choque:
            registry.revise_goal(run_id, "Lo que quiere ChatyGPT", base_revision=1)
        assert choque.value.details["current_revision"] == 2
        assert choque.value.details["current"] == "Lo que pidio Telegram"

        # Y el objetivo sigue siendo el de Telegram: un conflicto no deja el run a medias.
        assert registry.goal_of(run_id).current.text == "Lo que pidio Telegram"

        # Recuperacion: se relee, se decide, y se escribe sobre la revision vigente.
        vigente = registry.goal_of(run_id).current
        tercera = registry.revise_goal(
            run_id,
            "Lo de Telegram y ademas lo de ChatyGPT",
            base_revision=vigente.revision,
            reason="se juntan los dos encargos",
        )
        assert tercera.revision == 3
        assert registry.goal_of(run_id).current.text == "Lo de Telegram y ademas lo de ChatyGPT"

        # Se suelta y se le deja acabar antes de cerrar. Cancelar a mitad de una
        # verificacion deja viva la hebra que ejecuta el proceso, y el bucle de asyncio
        # se queda esperandola al cerrar: el test colgaria por como termina, no por lo
        # que mide.
        suelta.set()
        await _settle(registry, run_id)
        await registry.shutdown()

    asyncio.run(escenario())


# ---------------------------------------------------- E2E-10 · Crash Recovery


def _registro(session_id: str, estado: AgentStatus) -> SessionRecord:
    from athena.working_state import WorkingState

    return SessionRecord(
        session_id=session_id,
        workspace_id="ws-1",
        status=estado,
        working_memory=WorkingState(objective="Arreglar calc.add"),
    )


@pytest.mark.parametrize(
    "estado",
    [
        AgentStatus.RUNNING,
        AgentStatus.VERIFYING,
        AgentStatus.WAITING_PERMISSION,
    ],
)
def test_e2e_10_un_run_vivo_al_caerse_queda_por_recuperar_y_no_terminado(
    tmp_path: Path, estado: AgentStatus
) -> None:
    """Origen: MASTER_PROMPT. Caer en cualquier estado vivo deja trabajo por decidir.

    Los tres estados son los momentos que el encargo nombra —ejecutando (incluido lo que
    pase dentro de un subagente, que corre bajo el mismo run), esperando permiso,
    verificando— vistos desde lo unico que sobrevive al proceso: el estado persistido.
    Ninguno puede quedar como `completed` ni como `failed`: lo primero daria por bueno
    trabajo que quiza quedo a medias, y lo segundo culparia al cambio de que se apagara
    la maquina.
    """
    almacen = SqliteSessionStore(tmp_path / "sessions.db")

    async def escenario() -> None:
        await almacen.save(_registro("run-1", estado))

        # El proceso muere aqui. Al arrancar, lo que estaba vivo se marca.
        interrumpidos = await almacen.mark_interrupted()
        assert "run-1" in interrumpidos

        record = await almacen.load("run-1")
        assert record is not None
        assert record.status is AgentStatus.RECOVERY_PENDING
        assert record.resumable

    asyncio.run(escenario())


def test_e2e_10_un_run_terminado_no_se_reabre_al_reiniciar(tmp_path: Path) -> None:
    """Origen: MASTER_PROMPT. Recuperar es coherente: sólo alcanza a lo que estaba vivo.

    El complemento del anterior, y la mitad que hace que el anterior signifique algo: si
    `mark_interrupted` tocara tambien lo terminado, «por recuperar» dejaria de distinguir
    nada y la lista de recuperacion incluiria cada run que hubiera pasado por la maquina.
    """
    almacen = SqliteSessionStore(tmp_path / "sessions.db")

    async def escenario() -> None:
        await almacen.save(_registro("acabado", AgentStatus.COMPLETED))
        await almacen.save(_registro("vivo", AgentStatus.RUNNING))

        await almacen.mark_interrupted()

        acabado = await almacen.load("acabado")
        assert acabado is not None and acabado.status is AgentStatus.COMPLETED
        vivo = await almacen.load("vivo")
        assert vivo is not None and vivo.status is AgentStatus.RECOVERY_PENDING

    asyncio.run(escenario())


def test_e2e_10_lo_que_solo_vivia_en_el_proceso_no_sobrevive_a_el(tmp_path: Path) -> None:
    """Origen: MASTER_PROMPT. Las activaciones locales no aparecen falsamente vivas.

    Un registro nuevo no hereda runs: `live_ids()` sale vacio aunque el almacen tenga
    trabajo por recuperar. La distincion es la que separa «hay algo que decidir» de «hay
    algo corriendo», y confundirlas haria que un cliente esperase eventos de un bucle que
    ya no existe.
    """

    async def escenario() -> None:
        almacen = SqliteSessionStore(tmp_path / "sessions.db")
        await almacen.save(_registro("run-1", AgentStatus.RUNNING))
        await almacen.mark_interrupted()

        registry = RunRegistry(
            _Scripted(),
            InMemoryEventBus(),
            almacen,
            SqliteToolResultStore(tmp_path / "results.db"),
        )
        assert registry.live_ids() == ()
        pendientes = await registry.list(AgentStatus.RECOVERY_PENDING)
        assert [record.session_id for record in pendientes] == ["run-1"]

        await registry.shutdown()

    asyncio.run(escenario())


def test_e2e_10_una_aprobacion_de_antes_del_reinicio_ya_no_vale(tmp_path: Path) -> None:
    """Origen: MASTER_PROMPT. Las aprobaciones caducas se invalidan al reiniciar.

    Las peticiones de permiso viven **solo** en el proceso, y eso es la garantia y no un
    descuido: una respuesta guardada en disco autorizaria despues de un reinicio una
    accion que nadie ha vuelto a plantear, sobre un workspace que puede haber cambiado.
    Un registro nuevo no conoce ningun `request_id` de antes.
    """
    import time

    workspace = Workspace.from_path(tmp_path, "ws")

    async def escenario() -> None:
        antes = ApprovalRegistry()
        pendiente = PendingApproval(
            request_id="req-1",
            run_id="run-1",
            request=_peticion(
                workspace, "write_file", tier=RiskTier.R1_WORKSPACE_WRITE, solo_lectura=False
            ),
            future=asyncio.get_running_loop().create_future(),
            deadline_monotonic=time.monotonic() + 300.0,
        )
        antes.register(pendiente)
        assert antes.get("req-1") is not None
        assert antes.pending_for("run-1") == (pendiente,)

        # El proceso muere. Lo que viene despues es un registro nuevo, y no sabe nada.
        despues = ApprovalRegistry()
        assert despues.get("req-1") is None, "una aprobacion sobrevivio al proceso"
        assert despues.pending_for("run-1") == ()

        pendiente.future.cancel()

    asyncio.run(escenario())
