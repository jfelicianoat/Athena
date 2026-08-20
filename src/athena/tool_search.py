"""The tool that finds the other tools.

`ToolSearchTool` is itself a core tool — it has to be, since it is how anything deferred
becomes reachable. It returns schemas for matching deferred tools; the loop records what
was revealed so those schemas travel with subsequent turns.
"""

from __future__ import annotations

from athena.cancellation import CancellationToken
from athena.errors import ToolValidationError
from athena.permissions import PermissionRequest, RiskLevel, RiskTier
from athena.registry import ToolRegistry
from athena.tools import ToolContext, ToolLoadPolicy, ToolResult, ToolSpec
from athena.types import JSONObject

#: Name the loop watches for, to learn which deferred tools became visible.
TOOL_SEARCH_NAME = "tool_search"


class ToolSearchTool:
    """Reads the registry. It executes nothing and changes nothing."""

    spec = ToolSpec(
        name=TOOL_SEARCH_NAME,
        description=(
            "Find tools that are not loaded by default. Search by what you are trying to "
            "do; the matching tools become available on the next turn."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        output_schema={"type": "object"},
        risk=RiskLevel.LOW,
        max_result_size_chars=12_000,
        load_policy=ToolLoadPolicy.CORE,
        search_hint="discover capabilities that are not already listed",
    )

    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

    def validate(self, arguments: JSONObject) -> JSONObject:
        unknown = set(arguments) - {"query", "limit"}
        if unknown:
            raise ToolValidationError(f"Unknown input fields: {', '.join(sorted(unknown))}")
        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ToolValidationError("query must be a non-empty string")
        return {"query": query, "limit": self._limit(arguments)}

    @staticmethod
    def _limit(arguments: JSONObject) -> int:
        value = arguments.get("limit", 5)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ToolValidationError("limit must be an integer")
        if value < 1 or value > 20:
            raise ToolValidationError("limit must be between 1 and 20")
        return value

    def is_read_only(self, arguments: JSONObject) -> bool:
        del arguments
        return True

    def is_destructive(self, arguments: JSONObject) -> bool:
        del arguments
        return False

    def is_concurrency_safe(self, arguments: JSONObject) -> bool:
        del arguments
        return True

    def permission(self, context: ToolContext, arguments: JSONObject) -> PermissionRequest:
        return PermissionRequest(
            tool_name=self.spec.name,
            operation="tool_search",
            action=f"search available tools for {arguments.get('query', '')!r}",
            workspace=context.workspace,
            risk=RiskLevel.LOW,
            tier=RiskTier.R0_READ_ONLY,
            is_read_only=True,
            is_destructive=False,
            is_concurrency_safe=True,
            reason="The agent asked which additional tools exist.",
            possible_effects=("Reads the tool registry", "Changes nothing"),
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
        query = arguments.get("query")
        if not isinstance(query, str):
            raise ToolValidationError("query must be a string")
        matches = self.registry.search(query, limit=self._limit(arguments))
        return ToolResult(
            {
                "query": query,
                "matches": [
                    {
                        "name": tool.spec.name,
                        "description": tool.spec.description,
                        "search_hint": tool.spec.search_hint,
                        "input_schema": tool.spec.input_schema,
                    }
                    for tool in matches
                ],
                "revealed": [tool.spec.name for tool in matches],
                "deferred_available": len(self.registry.deferred_names()),
            }
        )


__all__ = ["TOOL_SEARCH_NAME", "ToolSearchTool"]
