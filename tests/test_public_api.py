from __future__ import annotations

import athena


def test_foundational_contracts_are_public() -> None:
    expected = {
        "AgentEvent",
        "AgentLoop",
        "AgentState",
        "CancellationToken",
        "ContextBuilder",
        "EventBus",
        "GlobTool",
        "GrepTool",
        "ListDirectoryTool",
        "ModelCapabilities",
        "ModelEvent",
        "ModelProvider",
        "ModelRequest",
        "ModelResponse",
        "ModelToolCall",
        "PermissionDecision",
        "PermissionRequest",
        "ReadFileTool",
        "ReadRangeTool",
        "SessionState",
        "Tool",
        "ToolContext",
        "ToolEvent",
        "ToolExecutor",
        "ToolResult",
        "ToolResultReference",
        "ToolResultStore",
        "ToolRegistry",
        "VerificationResult",
        "Workspace",
    }

    assert expected <= set(athena.__all__)
