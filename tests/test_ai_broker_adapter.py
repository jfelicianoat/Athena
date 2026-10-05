from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from pathlib import Path

import pytest

from athena.adapters.ai_broker import AiBrokerModelProvider
from athena.agent_loop import AgentLoop, AgentLoopConfig, AgentRunStatus
from athena.cancellation import CancellationSource, CancellationToken
from athena.context import ContextBuilder
from athena.errors import (
    ModelAuthenticationBackendError,
    ModelAuthenticationError,
    ModelPermanentError,
    ModelTransientError,
)
from athena.events import EventName, InMemoryEventBus, RuntimeEvent
from athena.models import ModelMessage, ModelRequest, ModelRole, ModelToolCall
from athena.mutation_tools import workspace_mutation_tools
from athena.permissions import PermissionPolicy, PolicyPermissionEngine
from athena.registry import ToolRegistry
from athena.stores import InMemoryToolResultStore
from athena.tool_executor import ToolExecutor
from athena.types import JSONObject, JSONValue
from athena.verification import LoopCompletionVerificationPolicy
from athena.workspace import Workspace
from athena_desktop.presentacion import present_result


class _StubBroker(AiBrokerModelProvider):
    def __init__(self, result: JSONObject) -> None:
        super().__init__("http://broker.local:8765", "secret")
        self.result = result
        self.submission: Mapping[str, JSONValue] | None = None

    async def _call(
        self,
        method: str,
        path: str,
        body: Mapping[str, JSONValue] | None,
        cancellation: CancellationToken | None,
    ) -> tuple[int, JSONObject]:
        del cancellation
        if method == "POST":
            self.submission = body
            return 201, {"task_id": "task-1"}
        assert path == "/api/v1/tasks/task-1"
        return 200, {"status": "completed", "result": self.result}


class _SequencedBroker(AiBrokerModelProvider):
    def __init__(self, results: list[JSONObject]) -> None:
        super().__init__("http://broker.local:8765", "secret")
        self.results = results
        self.task = 0

    async def _call(
        self,
        method: str,
        path: str,
        body: Mapping[str, JSONValue] | None,
        cancellation: CancellationToken | None,
    ) -> tuple[int, JSONObject]:
        del path, body, cancellation
        if method == "POST":
            self.task += 1
            return 201, {"task_id": f"task-{self.task}"}
        return 200, {"status": "completed", "result": self.results[self.task - 1]}


def _write_tool() -> JSONObject:
    return {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write text to a workspace file",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    }


def test_ai_broker_advertises_tool_calls() -> None:
    provider = _StubBroker({"assistant_content": "unused"})

    assert provider.capabilities().tool_calls is True


class _CredentialBroker(AiBrokerModelProvider):
    def __init__(self, *responses: tuple[int, JSONObject] | ModelTransientError) -> None:
        super().__init__("http://broker.local:8765", "secret")
        self.responses = list(responses)
        self.paths: list[str] = []

    async def _call(
        self,
        method: str,
        path: str,
        body: Mapping[str, JSONValue] | None,
        cancellation: CancellationToken | None,
    ) -> tuple[int, JSONObject]:
        assert method == "GET" and body is None
        assert cancellation is not None
        cancellation.raise_if_cancelled()
        self.paths.append(path)
        response = self.responses.pop(0)
        if isinstance(response, ModelTransientError):
            raise response
        return response


@pytest.mark.parametrize(
    "status,payload,accepted,message",
    [
        (200, {"authenticated": True, "auth_required": True}, True, "acepta el token"),
        (200, {"authenticated": True, "auth_required": False}, True, "no exige credenciales"),
        (200, {"authenticated": False, "auth_required": True}, False, "incompatible"),
        (200, {"authenticated": True}, False, "incompatible"),
        (200, {"authenticated": True, "auth_required": "false"}, False, "incompatible"),
        (401, {}, False, "rechaza el token"),
        (403, {}, False, "rechaza el token"),
        (503, {"code": "ADMIN_AUTH_BACKEND_UNAVAILABLE"}, False, "autenticación"),
        (503, {"detail": "ADMIN_AUTH_BACKEND_UNAVAILABLE"}, False, "almacén"),
        (403, {"detail": "ADMIN_AUTH_REQUIRED"}, False, "rechaza el token"),
        (500, {}, False, "HTTP 500"),
    ],
)
def test_credentials_use_auth_check_and_distinguish_disabled_authentication(
    status: int, payload: JSONObject, accepted: bool, message: str
) -> None:
    provider = _CredentialBroker((status, payload))
    result, detail = asyncio.run(provider.verify_credentials(CancellationSource().token))
    assert result is accepted
    assert message in detail
    if status == 200 and payload.get("auth_required") is False:
        assert "no se ha validado" in detail
    assert provider.paths == ["/api/v1/auth/check"]


@pytest.mark.parametrize("status,accepted", [(200, True), (403, False), (503, False)])
def test_credentials_keep_protected_fallback_only_for_old_brokers(
    status: int, accepted: bool
) -> None:
    provider = _CredentialBroker((404, {}), (status, {}))
    result, _ = asyncio.run(provider.verify_credentials(CancellationSource().token))
    assert result is accepted
    assert provider.paths == ["/api/v1/auth/check", "/api/v1/dashboard/tasks?limit=1"]


def test_credentials_report_transport_failure() -> None:
    provider = _CredentialBroker(ModelTransientError("offline"))
    accepted, message = asyncio.run(provider.verify_credentials(CancellationSource().token))
    assert not accepted and "No se pudo contactar" in message
    assert provider.paths == ["/api/v1/auth/check"]


class _AuthenticationTaskBroker(AiBrokerModelProvider):
    def __init__(self, status: int, payload: JSONObject, *, during_submit: bool = False) -> None:
        super().__init__("http://broker.local:8765", "secret", max_wait_seconds=0.2)
        self.status = status
        self.payload = payload
        self.during_submit = during_submit
        self.calls: list[tuple[str, str]] = []

    async def _call(
        self,
        method: str,
        path: str,
        body: Mapping[str, JSONValue] | None,
        cancellation: CancellationToken | None,
    ) -> tuple[int, JSONObject]:
        self.calls.append((method, path))
        if cancellation is not None:
            cancellation.raise_if_cancelled()
        if method == "POST" and not self.during_submit:
            return 202, {"task_id": "retained-task"}
        return self.status, self.payload


@pytest.mark.parametrize("during_submit", [False, True])
@pytest.mark.parametrize(
    "status,payload,error_type,action",
    [
        (401, {}, ModelAuthenticationError, "renew_token"),
        (403, {"error": {"code": "ADMIN_AUTH_REQUIRED"}}, ModelAuthenticationError, "renew_token"),
        (
            503,
            {"error": {"code": "ADMIN_AUTH_BACKEND_UNAVAILABLE"}},
            ModelAuthenticationBackendError,
            "restore_auth_backend",
        ),
        (
            503,
            {"detail": {"code": "ADMIN_AUTH_BACKEND_UNAVAILABLE"}},
            ModelAuthenticationBackendError,
            "restore_auth_backend",
        ),
        (
            503,
            {"code": "ADMIN_AUTH_BACKEND_UNAVAILABLE"},
            ModelAuthenticationBackendError,
            "restore_auth_backend",
        ),
        # The shape the live broker uses: FastAPI's `detail` as a bare code string.
        (403, {"detail": "ADMIN_AUTH_REQUIRED"}, ModelAuthenticationError, "renew_token"),
        (
            503,
            {"detail": "ADMIN_AUTH_BACKEND_UNAVAILABLE"},
            ModelAuthenticationBackendError,
            "restore_auth_backend",
        ),
    ],
)
def test_authentication_problems_preserve_broker_work_and_do_not_wait_for_timeout(
    during_submit: bool,
    status: int,
    payload: JSONObject,
    error_type: type[ModelAuthenticationError],
    action: str,
) -> None:
    provider = _AuthenticationTaskBroker(status, payload, during_submit=during_submit)
    with pytest.raises(error_type) as caught:
        asyncio.run(
            provider.complete(
                ModelRequest(messages=(ModelMessage(ModelRole.USER, "explain"),)),
                CancellationSource().token,
            )
        )
    error = caught.value
    assert error.details["http_status"] == status
    assert error.details["action"] == action
    assert "secret" not in str(error.details) + error.message
    assert provider.calls == [("POST", "/api/v1/tasks")] + (
        [] if during_submit else [("GET", "/api/v1/tasks/retained-task")]
    )
    if during_submit:
        assert "task_preserved" not in error.details
    else:
        assert error.details["task"] == "retained-task"
        assert error.details["task_preserved"] is True


def test_other_503_errors_keep_existing_transport_recovery() -> None:
    provider = _AuthenticationTaskBroker(503, {"error": {"code": "PROVIDER_UNAVAILABLE"}})
    with pytest.raises(ModelTransientError):
        asyncio.run(provider.complete(ModelRequest(messages=()), CancellationSource().token))
    assert provider.calls[-1] == ("DELETE", "/api/v1/tasks/retained-task")


@pytest.mark.parametrize(
    "status,code,expected_error,expected_step",
    [
        (403, "ADMIN_AUTH_REQUIRED", ModelAuthenticationError, "Renueva el token"),
        (
            503,
            "ADMIN_AUTH_BACKEND_UNAVAILABLE",
            ModelAuthenticationBackendError,
            "Restablece el almacén",
        ),
    ],
)
def test_authentication_problem_reaches_the_desktop_without_retrying_the_task(
    tmp_path: Path,
    status: int,
    code: str,
    expected_error: type[ModelAuthenticationError],
    expected_step: str,
) -> None:
    async def scenario() -> None:
        provider = _AuthenticationTaskBroker(status, {"error": {"code": code}})
        workspace = Workspace.from_path(tmp_path)
        event_bus = InMemoryEventBus()
        events: list[RuntimeEvent] = []
        event_bus.subscribe(events.append)
        registry = ToolRegistry(())
        loop = AgentLoop(
            provider,
            registry,
            ToolExecutor(
                registry,
                PolicyPermissionEngine(PermissionPolicy()),
                InMemoryToolResultStore(),
                event_bus,
            ),
            ContextBuilder(workspace),
            event_bus,
        )
        result = await loop.run("Describe the project", workspace, CancellationSource().token)
        assert isinstance(result.error, expected_error)
        assert len(provider.calls) == 2
        failure = next(event for event in events if event.name is EventName.MODEL_FAILED)
        assert failure.payload["retrying"] is False
        view = present_result(result, task_kind="question")
        assert view.tone == "warn"
        assert "se ha conservado" in view.explanation
        assert expected_step in view.next_steps[0]
        assert "broker_task=retained-task" in view.technical

    asyncio.run(scenario())


def test_ai_broker_turns_structured_decisions_into_tool_calls() -> None:
    async def scenario() -> None:
        provider = _StubBroker(
            {
                "assistant_content": (
                    '{"kind":"tool_calls","tool_calls":[{"call_id":"call-1",'
                    '"name":"write_file","arguments":{"path":"impresiones.txt",'
                    '"content":"Una impresión contemporánea."}}]}'
                )
            }
        )
        response = await provider.complete(
            ModelRequest(
                messages=(ModelMessage(ModelRole.USER, "Escribe mis impresiones"),),
                tools=(_write_tool(),),
            ),
            CancellationSource().token,
        )

        assert response.finish_reason == "tool_calls"
        assert response.tool_calls == (
            ModelToolCall(
                "call-1",
                "write_file",
                {
                    "path": "impresiones.txt",
                    "content": "Una impresión contemporánea.",
                },
            ),
        )
        assert provider.submission is not None
        output = provider.submission["output"]
        assert isinstance(output, Mapping)
        assert output["format"] == "json"
        content = provider.submission["content"]
        assert isinstance(content, Mapping)
        assert "write_file" in str(content["prompt"])

    asyncio.run(scenario())


def test_ai_broker_preserves_tool_call_history_in_the_next_prompt() -> None:
    async def scenario() -> None:
        provider = _StubBroker(
            {"assistant_content": '{"kind":"message","message":"Archivo creado."}'}
        )
        response = await provider.complete(
            ModelRequest(
                messages=(
                    ModelMessage(
                        ModelRole.ASSISTANT,
                        "",
                        tool_calls=(
                            ModelToolCall("call-1", "write_file", {"path": "impresiones.txt"}),
                        ),
                    ),
                    ModelMessage(
                        ModelRole.TOOL,
                        '{"ok":true}',
                        name="write_file",
                        tool_call_id="call-1",
                    ),
                ),
                tools=(_write_tool(),),
            ),
            CancellationSource().token,
        )

        assert response.content == "Archivo creado."
        assert response.finish_reason == "stop"
        assert provider.submission is not None
        content = provider.submission["content"]
        assert isinstance(content, Mapping)
        prompt = str(content["prompt"])
        assert "call-1" in prompt
        assert "write_file" in prompt

    asyncio.run(scenario())


def test_ai_broker_retries_when_a_required_tool_turn_returns_an_empty_message() -> None:
    async def scenario() -> None:
        provider = _StubBroker({"assistant_content": '{"kind":"message"}'})

        with pytest.raises(ModelTransientError, match="required a tool call"):
            await provider.complete(
                ModelRequest(
                    messages=(ModelMessage(ModelRole.USER, "Crea un archivo"),),
                    tools=(_write_tool(),),
                    options={"tool_choice": "required"},
                ),
                CancellationSource().token,
            )

        assert provider.submission is not None
        output = provider.submission["output"]
        assert isinstance(output, Mapping)
        schema = output["json_schema"]
        assert isinstance(schema, Mapping)
        assert schema["required"] == ["kind", "tool_calls"]

    asyncio.run(scenario())


def test_ai_broker_preserves_plain_model_response_when_a_tool_was_required() -> None:
    async def scenario() -> None:
        refusal = "No puedo crear el archivo solicitado."
        provider = _StubBroker({"assistant_content": refusal})

        with pytest.raises(ModelTransientError) as failure:
            await provider.complete(
                ModelRequest(
                    messages=(ModelMessage(ModelRole.USER, "Crea un archivo"),),
                    tools=(_write_tool(),),
                    options={"tool_choice": "required"},
                ),
                CancellationSource().token,
            )

        assert failure.value.details["model_response"] == refusal
        assert refusal in failure.value.message

    asyncio.run(scenario())


def test_prose_instead_of_a_decision_is_transient_even_on_a_free_turn() -> None:
    """One badly shaped reply is not a broken provider.

    A real run died six seconds in because the routed model answered a "create a kanban
    app" objective with a markdown comparison of Electron, PyQt and .NET instead of the
    decision object. It was classified permanent — no fallback configured means abort —
    so a single sample ended the run with no second attempt. The broker had accepted the
    task, dispatched it and answered: nothing about that says it cannot serve the
    request. `RecoveryPolicy` bounds the retries, so transient here is still finite.
    """

    async def scenario() -> None:
        prose = "### 1. Recomendaciones: Electron, PyQt o .NET. Cual prefieres?"
        provider = _StubBroker({"assistant_content": prose})

        with pytest.raises(ModelTransientError, match="structured Athena tool decision"):
            await provider.complete(
                ModelRequest(
                    messages=(ModelMessage(ModelRole.USER, "Crea un kanban"),),
                    tools=(_write_tool(),),
                ),
                CancellationSource().token,
            )

    asyncio.run(scenario())


class _FailingBroker(AiBrokerModelProvider):
    """Un broker cuya tarea termina en `failed`, con el error tal y como lo publica."""

    def __init__(self, error: JSONObject) -> None:
        super().__init__("http://broker.local:8765", "secret")
        self.error = error

    async def _call(
        self,
        method: str,
        path: str,
        body: Mapping[str, JSONValue] | None,
        cancellation: CancellationToken | None,
    ) -> tuple[int, JSONObject]:
        del body, cancellation
        if method == "POST":
            return 201, {"task_id": "task-1"}
        assert path == "/api/v1/tasks/task-1"
        return 200, {"status": "failed", "error": self.error}


def test_a_failure_the_broker_calls_retryable_is_not_the_end_of_the_run() -> None:
    """El broker sabe cuando su fallo se puede reintentar, y lo dice.

    Un `TASK_TIMEOUT` esperando a que un modelo de 30B acabe de cargarse desde disco
    llega con `retryable: true`. Se estaba leyendo como permanente, que sin proveedor de
    respaldo significa abortar: el run moria por una espera, y la siguiente peticion
    —con el modelo ya en memoria— habria salido bien.
    """

    async def scenario() -> None:
        provider = _FailingBroker(
            {
                "code": "TASK_TIMEOUT",
                "message": "La tarea supero el timeout efectivo de 600 segundos.",
                "retryable": True,
            }
        )

        with pytest.raises(ModelTransientError) as failure:
            await provider.complete(
                ModelRequest(messages=(ModelMessage(ModelRole.USER, "Hola"),)),
                CancellationSource().token,
            )

        detalle = failure.value.details["detail"]
        assert isinstance(detalle, str)
        assert "TASK_TIMEOUT" in detalle, "el detalle llega como objeto y antes se perdia entero"

    asyncio.run(scenario())


def test_a_failure_without_that_promise_stays_permanent() -> None:
    async def scenario() -> None:
        provider = _FailingBroker({"code": "CONTRACT_VALIDATION_FAILED", "message": "no vale"})

        with pytest.raises(ModelPermanentError):
            await provider.complete(
                ModelRequest(messages=(ModelMessage(ModelRole.USER, "Hola"),)),
                CancellationSource().token,
            )

    asyncio.run(scenario())


def test_a_decision_that_names_no_tool_is_worth_asking_again() -> None:
    """El modelo anuncio herramientas y no nombro ninguna.

    Salio tal cual de un run real contra `qwen3-coder:30b`:
    `{"kind":"tool_calls","tool_calls":[]}`. El proveedor esta perfectamente: acepto la
    tarea, la despacho y contesto. Lo unico que fallo fue la respuesta, y matar el run
    por ella dejaba al usuario con «Fallido» y sin nada hecho.
    """

    async def scenario() -> None:
        provider = _StubBroker({"assistant_content": '{"kind":"tool_calls","tool_calls":[]}'})

        with pytest.raises(ModelTransientError, match="empty tool decision"):
            await provider.complete(
                ModelRequest(
                    messages=(ModelMessage(ModelRole.USER, "Escribe una nota"),),
                    tools=(_write_tool(),),
                ),
                CancellationSource().token,
            )

    asyncio.run(scenario())


def test_a_decision_of_an_unknown_kind_is_also_worth_asking_again() -> None:
    async def scenario() -> None:
        provider = _StubBroker({"assistant_content": '{"kind":"thinking","message":"mmm"}'})

        with pytest.raises(ModelTransientError, match="unknown Athena decision kind"):
            await provider.complete(
                ModelRequest(
                    messages=(ModelMessage(ModelRole.USER, "Escribe una nota"),),
                    tools=(_write_tool(),),
                ),
                CancellationSource().token,
            )

    asyncio.run(scenario())


def test_ai_broker_cannot_invent_authority_for_an_unoffered_tool() -> None:
    async def scenario() -> None:
        provider = _StubBroker(
            {
                "assistant_content": (
                    '{"kind":"tool_calls","tool_calls":[{"name":"delete_everything",'
                    '"arguments":{}}]}'
                )
            }
        )

        with pytest.raises(ModelPermanentError, match="did not offer"):
            await provider.complete(
                ModelRequest(
                    messages=(ModelMessage(ModelRole.USER, "Hazlo"),),
                    tools=(_write_tool(),),
                ),
                CancellationSource().token,
            )

    asyncio.run(scenario())


def test_athena_executes_a_file_tool_selected_through_ai_broker(tmp_path: Path) -> None:
    async def scenario() -> None:
        provider = _SequencedBroker(
            [
                {
                    "assistant_content": (
                        '{"kind":"message","message":"Estas son mis impresiones."}'
                    )
                },
                {
                    "assistant_content": (
                        '{"kind":"tool_calls","tool_calls":[{"call_id":"write-1",'
                        '"name":"write_file","arguments":{"path":"impresiones.txt",'
                        '"content":"Una impresión contemporánea."}}]}'
                    )
                },
                {
                    "assistant_content": (
                        '{"kind":"message","message":"He creado impresiones.txt."}'
                    )
                },
            ]
        )
        workspace = Workspace.from_path(tmp_path)
        event_bus = InMemoryEventBus()
        registry = ToolRegistry(workspace_mutation_tools(event_bus))
        executor = ToolExecutor(
            registry,
            PolicyPermissionEngine(PermissionPolicy(allow_workspace_writes=True)),
            InMemoryToolResultStore(),
            event_bus,
        )
        loop = AgentLoop(
            provider,
            registry,
            executor,
            ContextBuilder(workspace),
            event_bus,
            verification=LoopCompletionVerificationPolicy(),
            config=AgentLoopConfig(require_workspace_change=True),
        )

        result = await loop.run(
            "Escribe un fichero con impresiones sobre un cuadro contemporáneo",
            workspace,
            CancellationSource().token,
        )

        assert result.status is AgentRunStatus.COMPLETED
        assert (tmp_path / "impresiones.txt").read_text(encoding="utf-8") == (
            "Una impresión contemporánea."
        )

    asyncio.run(scenario())


class _SlowPollingBroker(AiBrokerModelProvider):
    """Un broker vivo que nunca termina, y que tarda en decir que sigue generando.

    Es el caso real que se midió: con la cola llena, cada consulta de estado tarda entre
    ocho y veinte segundos. Lo que importa no es que sea lento, sino que el tiempo se
    vaya en la consulta y no en la espera entre consultas.
    """

    def __init__(self, *, poll_cost: float, max_wait: float) -> None:
        super().__init__(
            "http://broker.local:8765",
            "secret",
            poll_interval_seconds=0.01,
            max_wait_seconds=max_wait,
        )
        self.poll_cost = poll_cost
        self.polls = 0

    async def _call(
        self,
        method: str,
        path: str,
        body: Mapping[str, JSONValue] | None,
        cancellation: CancellationToken | None,
    ) -> tuple[int, JSONObject]:
        del path, body, cancellation
        if method == "POST":
            return 201, {"task_id": "task-1"}
        if method == "DELETE":
            return 200, {}
        self.polls += 1
        await asyncio.sleep(self.poll_cost)
        return 200, {"status": "generating"}


def test_a_broker_that_never_finishes_is_given_up_on_in_real_time() -> None:
    """El techo son segundos de reloj, no la suma de las esperas entre consultas.

    Sumando los sleeps, un sondeo que tarda veinte segundos con un intervalo de uno hacía
    que un techo de diez minutos se cumpliese al cabo de tres horas. Un run se quedaba
    colgado sin evento y sin fallo mientras alguien lo miraba.
    """

    async def scenario() -> None:
        broker = _SlowPollingBroker(poll_cost=0.05, max_wait=0.2)
        request = ModelRequest(messages=(ModelMessage(ModelRole.USER, "hola"),))
        empezado = time.monotonic()
        with pytest.raises(ModelTransientError, match="did not answer"):
            await broker.complete(request, CancellationSource().token)
        transcurrido = time.monotonic() - empezado

        # Con la cuenta vieja habrían hecho falta veinte vueltas —más de un segundo— para
        # acumular 0,2 s de sleeps. Con el reloj bastan cuatro.
        assert transcurrido < 1.0
        assert broker.polls < 12

    asyncio.run(scenario())


def test_a_deployment_preference_may_be_rerouted_but_a_run_s_choice_may_not() -> None:
    """La diferencia entre nombrar un modelo y elegirlo.

    El ajuste del despliegue se nombra sin imponerse: el broker es quien enruta y quien
    sabe que hay caido. Una eleccion explicita de este run es otra cosa — un selector que
    el broker puede ignorar en silencio no es un selector, y quien pidio un modelo y
    recibio otro no se entera hasta que el trabajo sale mal.
    """

    async def scenario() -> None:
        result: JSONObject = {"assistant_content": "listo", "usage": {}}

        broker = _StubBroker(result)
        broker._preferred_model = "modelo-del-despliegue"
        await broker.complete(
            ModelRequest(messages=(ModelMessage(ModelRole.USER, "hola"),)),
            CancellationSource().token,
        )
        assert broker.submission is not None
        requirements = broker.submission["model_requirements"]
        assert requirements == {
            "preferred_model": "modelo-del-despliegue",
            "fallback_allowed": True,
        }

        chosen = _StubBroker(result)
        chosen._preferred_model = "modelo-del-despliegue"
        await chosen.complete(
            ModelRequest(
                messages=(ModelMessage(ModelRole.USER, "hola"),),
                model="el-que-pidio-la-persona",
            ),
            CancellationSource().token,
        )
        assert chosen.submission is not None
        assert chosen.submission["model_requirements"] == {
            "preferred_model": "el-que-pidio-la-persona",
            "fallback_allowed": False,
        }

    asyncio.run(scenario())
