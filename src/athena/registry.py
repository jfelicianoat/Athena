"""Tool discovery without deferred loading or provider knowledge."""

from __future__ import annotations

from collections.abc import Iterable

from athena.errors import ToolValidationError
from athena.tools import Tool
from athena.types import JSONObject


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
            raise ToolValidationError(
                f"Unknown tool: {name}", details={"tool_name": name}
            ) from exc

    def definitions(self) -> tuple[JSONObject, ...]:
        definitions: list[JSONObject] = []
        for tool in self._tools.values():
            definitions.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool.spec.name,
                        "description": tool.spec.description,
                        "parameters": tool.spec.input_schema,
                    },
                }
            )
        return tuple(definitions)

    def names(self) -> tuple[str, ...]:
        return tuple(self._tools)
