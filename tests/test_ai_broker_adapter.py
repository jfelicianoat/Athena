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
from athena.errors import ModelPermanentError, ModelTransientError
from athena.events import InMemoryEventBus
from athena.models import ModelMessage, ModelRequest, ModelRole, ModelToolCall
from athena.mutation_tools import workspace_mutation_tools
from athena.permissions import PermissionPolicy, PolicyPermissionEngine
from athena.registry import ToolRegistry
from athena.stores import InMemoryToolResultStore
from athena.tool_executor import ToolExecutor
from athena.types import JSONObject, JSONValue
from athena.verification import LoopCompletionVerificationPolicy
from athena.workspace import Workspace


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
