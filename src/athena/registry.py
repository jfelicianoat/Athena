"""Tool discovery, with deferred loading built on the H0 contracts.

A model that is shown every schema pays for every schema on every turn. `ToolSpec` has
carried `load_policy` and `search_hint` since H0 precisely so that this could be
implemented later without changing any tool: core tools are always visible, deferred tools
are discoverable by search and only then revealed.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable

from athena.errors import ToolValidationError
from athena.tools import Tool, ToolLoadPolicy
from athena.types import JSONObject


def _definition(tool: Tool) -> JSONObject:
    return {
        "type": "function",
        "function": {
            "name": tool.spec.name,
            "description": tool.spec.description,
            "parameters": tool.spec.input_schema,
        },
    }


class ToolRegistry:
    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        name = tool.spec.name
        if name in self._tools:
            raise ValueError(f"Tool already registered: {name}")
        self._tools[name] = tool

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ToolValidationError(f"Unknown tool: {name}", details={"tool_name": name}) from exc

    def definitions(self, revealed: Collection[str] = ()) -> tuple[JSONObject, ...]:
        """Schemas to send this turn: every core tool, plus whatever search revealed."""
        return tuple(
            _definition(tool)
            for tool in self._tools.values()
            if tool.spec.load_policy is ToolLoadPolicy.CORE or tool.spec.name in revealed
        )

    def names(self) -> tuple[str, ...]:
        return tuple(self._tools)

    def core_names(self) -> tuple[str, ...]:
        return tuple(
            name
            for name, tool in self._tools.items()
            if tool.spec.load_policy is ToolLoadPolicy.CORE
        )

    def deferred_names(self) -> tuple[str, ...]:
        return tuple(
            name
            for name, tool in self._tools.items()
            if tool.spec.load_policy is ToolLoadPolicy.DEFERRED
        )

    def search(self, query: str, *, limit: int = 5) -> tuple[Tool, ...]:
        """Rank deferred tools against a query, using the H0 `search_hint`.

        Only deferred tools are searchable: a core tool is already in front of the model,
        so returning it would spend a turn to reveal what was never hidden.
        """
        words = {word for word in query.lower().split() if word}
        scored: list[tuple[int, str, Tool]] = []
        for name, tool in self._tools.items():
            if tool.spec.load_policy is not ToolLoadPolicy.DEFERRED:
                continue
            haystack = " ".join(
                part for part in (name, tool.spec.description, tool.spec.search_hint or "") if part
            ).lower()
            score = sum(3 if word in name.lower() else 1 for word in words if word in haystack)
            if score:
                scored.append((score, name, tool))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return tuple(tool for _, _, tool in scored[:limit])

    def describe(self, names: Collection[str]) -> tuple[JSONObject, ...]:
        return tuple(_definition(self._tools[name]) for name in names if name in self._tools)
