from __future__ import annotations

import asyncio
from pathlib import Path

from athena.cancellation import CancellationSource, CancellationToken
from athena.permissions import PermissionRequest, RiskLevel, RiskTier
from athena.tools import Tool, ToolContext, ToolLoadPolicy, ToolResult, ToolSpec
from athena.types import JSONObject
from athena.workspace import Workspace


class EchoTool:
    spec = ToolSpec(
        name="echo",
        description="Return validated text without external effects.",
        input_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
        output_schema={"type": "object", "properties": {"text": {"type": "string"}}},
        risk=RiskLevel.LOW,
        max_result_size_chars=1_000,
        load_policy=ToolLoadPolicy.CORE,
        search_hint="echo text",
    )

    def validate(self, arguments: JSONObject) -> JSONObject:
        if not isinstance(arguments.get("text"), str):
            raise ValueError("text is required")
        return arguments

    def permission(self, context: ToolContext, arguments: JSONObject) -> PermissionRequest:
        return PermissionRequest(
            tool_name=self.spec.name,
            operation="echo",
            workspace=context.workspace,
            risk=self.spec.risk,
            tier=RiskTier.R0_READ_ONLY,
            is_read_only=self.is_read_only(arguments),
            is_destructive=self.is_destructive(arguments),
            arguments=arguments,
        )

    async def execute(
        self,
        context: ToolContext,
        arguments: JSONObject,
        cancellation: CancellationToken,
    ) -> ToolResult:
        del context
        cancellation.raise_if_cancelled()
        return ToolResult(output=self.validate(arguments))

    def is_read_only(self, arguments: JSONObject) -> bool:
        del arguments
        return True

    def is_destructive(self, arguments: JSONObject) -> bool:
        del arguments
        return False

    def is_concurrency_safe(self, arguments: JSONObject) -> bool:
        del arguments
        return True


def test_tool_exposes_full_metadata_and_schema_contract() -> None:
    tool = EchoTool()

    assert isinstance(tool, Tool)
    assert tool.spec.input_schema["required"] == ["text"]
    assert tool.spec.output_schema["type"] == "object"
    assert tool.spec.max_result_size_chars == 1_000
    assert tool.spec.load_policy is ToolLoadPolicy.CORE
    assert tool.spec.search_hint == "echo text"
    assert tool.is_read_only({"text": "hello"})
    assert not tool.is_destructive({"text": "hello"})
    assert tool.is_concurrency_safe({"text": "hello"})


def test_tool_execution_and_permission_are_structured(tmp_path: Path) -> None:
    async def scenario() -> None:
        tool = EchoTool()
        context = ToolContext(
            session_id="s-1",
            workspace=Workspace("w-1", tmp_path),
            call_id="c-1",
        )
        arguments = tool.validate({"text": "hello"})
        permission = tool.permission(context, arguments)
        result = await tool.execute(context, arguments, CancellationSource().token)

        assert permission.workspace == context.workspace
        assert permission.risk is RiskLevel.LOW
        assert result.output == {"text": "hello"}

    asyncio.run(scenario())
