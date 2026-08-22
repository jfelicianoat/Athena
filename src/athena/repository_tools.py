"""Read-only, workspace-confined tools for incremental repository investigation."""

from __future__ import annotations

import re
from itertools import islice
from typing import ClassVar

from athena.cancellation import CancellationToken
from athena.errors import ToolExecutionError, ToolValidationError
from athena.permissions import PermissionRequest, RiskLevel, RiskTier
from athena.tool_projection import (
    DISPLAY_ITEM_LIMIT,
    MODEL_TEXT_LIMIT,
    DisplayView,
    ModelView,
    ResultKind,
    ToolProjection,
)
from athena.tools import Tool, ToolContext, ToolResult, ToolSpec
from athena.types import JSONObject

_MAX_TEXT_FILE_BYTES = 2_000_000


def _reject_unknown(arguments: JSONObject, allowed: set[str]) -> None:
    unknown = set(arguments) - allowed
    if unknown:
        raise ToolValidationError(f"Unknown input fields: {', '.join(sorted(unknown))}")


def _string(arguments: JSONObject, name: str, *, default: str | None = None) -> str:
    value = arguments.get(name, default)
    if not isinstance(value, str) or not value:
        raise ToolValidationError(f"{name} must be a non-empty string")
    return value


def _integer(
    arguments: JSONObject,
    name: str,
    *,
    default: int,
    minimum: int = 1,
    maximum: int,
) -> int:
    value = arguments.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolValidationError(f"{name} must be an integer")
    if value < minimum or value > maximum:
        raise ToolValidationError(f"{name} must be between {minimum} and {maximum}")
    return value


class _ReadOnlyTool:
    spec: ClassVar[ToolSpec]

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
        resource = self._permission_resource(context, arguments)
        return PermissionRequest(
            tool_name=self.spec.name,
            operation=self.spec.name,
            action=f"{self.spec.name} {resource}",
            workspace=context.workspace,
            risk=RiskLevel.LOW,
            tier=RiskTier.R0_READ_ONLY,
            is_read_only=True,
            is_destructive=False,
            is_concurrency_safe=True,
            reason="The agent requested read-only access inside the workspace.",
            possible_effects=("Reads workspace content", "Changes nothing"),
            resources=(resource,),
            arguments=arguments,
        )

    def _permission_resource(self, context: ToolContext, arguments: JSONObject) -> str:
        path = _string(arguments, "path", default=".")
        return str(context.workspace.resolve(path))


class ReadFileTool(_ReadOnlyTool):
    spec = ToolSpec(
        name="read_file",
        description="Read one UTF-8 text file. Prefer read_range after locating relevant lines.",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
                "line_count": {"type": "integer"},
            },
            "required": ["path", "content", "line_count"],
            "additionalProperties": False,
        },
        risk=RiskLevel.LOW,
        max_result_size_chars=12_000,
        search_hint="read a known small text file",
    )

    def validate(self, arguments: JSONObject) -> JSONObject:
        _reject_unknown(arguments, {"path"})
        return {"path": _string(arguments, "path")}

    async def execute(
        self,
        context: ToolContext,
        arguments: JSONObject,
        cancellation: CancellationToken,
    ) -> ToolResult:
        cancellation.raise_if_cancelled()
        path = context.workspace.resolve(_string(arguments, "path"))
        if not path.is_file():
            raise ToolValidationError(f"Not a file: {arguments['path']}")
        if path.stat().st_size > _MAX_TEXT_FILE_BYTES:
            raise ToolValidationError("File is too large for read_file; use read_range")
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ToolExecutionError(f"Cannot read text file: {arguments['path']}") from exc
        cancellation.raise_if_cancelled()
        return ToolResult(
            {
                "path": context.workspace.relative(path),
                "content": content,
                "line_count": len(content.splitlines()),
            }
        )


class ReadRangeTool(_ReadOnlyTool):
    spec = ToolSpec(
        name="read_range",
        description="Read an inclusive 1-based line range from one UTF-8 text file.",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "start_line": {"type": "integer", "minimum": 1},
                "end_line": {"type": "integer", "minimum": 1},
            },
            "required": ["path", "start_line", "end_line"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "start_line": {"type": "integer"},
                "end_line": {"type": "integer"},
                "lines": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "line": {"type": "integer"},
                            "text": {"type": "string"},
                        },
                        "required": ["line", "text"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["path", "start_line", "end_line", "lines"],
            "additionalProperties": False,
        },
        risk=RiskLevel.LOW,
        max_result_size_chars=12_000,
        search_hint="read only the relevant lines after grep",
    )

    def validate(self, arguments: JSONObject) -> JSONObject:
        _reject_unknown(arguments, {"path", "start_line", "end_line"})
        start = _integer(arguments, "start_line", default=1, maximum=1_000_000)
        end = _integer(arguments, "end_line", default=start, maximum=1_000_000)
        if end < start:
            raise ToolValidationError("end_line must be greater than or equal to start_line")
        if end - start + 1 > 400:
            raise ToolValidationError("A read_range call may request at most 400 lines")
        return {"path": _string(arguments, "path"), "start_line": start, "end_line": end}

    async def execute(
        self,
        context: ToolContext,
        arguments: JSONObject,
        cancellation: CancellationToken,
    ) -> ToolResult:
        cancellation.raise_if_cancelled()
        path = context.workspace.resolve(_string(arguments, "path"))
        if not path.is_file():
            raise ToolValidationError(f"Not a file: {arguments['path']}")
        start = _integer(arguments, "start_line", default=1, maximum=1_000_000)
        end = _integer(arguments, "end_line", default=start, maximum=1_000_000)
        selected: list[JSONObject] = []
        actual_end = start - 1
        try:
            with path.open(encoding="utf-8") as handle:
                for number, line in enumerate(handle, start=1):
                    cancellation.raise_if_cancelled()
                    if number > end:
                        break
                    if number >= start:
                        selected.append({"line": number, "text": line.rstrip("\r\n")})
                        actual_end = number
        except (OSError, UnicodeError) as exc:
            raise ToolExecutionError(f"Cannot read text file: {arguments['path']}") from exc
        return ToolResult(
            {
                "path": context.workspace.relative(path),
                "start_line": start,
                "end_line": actual_end,
                "lines": selected,
            }
        )

    def project(self, result: ToolResult) -> ToolProjection:
        """Codigo numerado, que es como se lee el codigo.

        El caso general no sabe que esto son lineas de un fichero y las enseña como pares
        `line=1 text=...`. Al modelo eso le cuesta tokens en decoracion y le dificulta
        citar una linea por su numero, que es justo para lo que existe esta tool.
        """
        return _numbered(self.spec.name, result, _relative_path(result))


class ListDirectoryTool(_ReadOnlyTool):
    spec = ToolSpec(
        name="list_directory",
        description="List direct children of one directory without recursive loading.",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "default": "."},
                "max_entries": {"type": "integer", "minimum": 1, "maximum": 500},
            },
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "entries": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "type": {"type": "string", "enum": ["file", "directory"]},
                        },
                        "required": ["path", "type"],
                        "additionalProperties": False,
                    },
                },
                "truncated": {"type": "boolean"},
            },
            "required": ["path", "entries", "truncated"],
            "additionalProperties": False,
        },
        risk=RiskLevel.LOW,
        max_result_size_chars=12_000,
        search_hint="inspect one directory level",
    )

    def validate(self, arguments: JSONObject) -> JSONObject:
        _reject_unknown(arguments, {"path", "max_entries"})
        return {
            "path": _string(arguments, "path", default="."),
            "max_entries": _integer(arguments, "max_entries", default=200, maximum=500),
        }

    async def execute(
        self,
        context: ToolContext,
        arguments: JSONObject,
        cancellation: CancellationToken,
    ) -> ToolResult:
        path = context.workspace.resolve(_string(arguments, "path", default="."))
        if not path.is_dir():
            raise ToolValidationError(f"Not a directory: {arguments['path']}")
        maximum = _integer(arguments, "max_entries", default=200, maximum=500)
        entries: list[JSONObject] = []
        children = list(islice(path.iterdir(), maximum + 1))
        truncated = len(children) > maximum
        for child in sorted(children[:maximum], key=lambda item: item.name.casefold()):
            cancellation.raise_if_cancelled()
            canonical = context.workspace.resolve(child)
            entries.append(
                {
                    "path": context.workspace.relative(canonical),
                    "type": "directory" if canonical.is_dir() else "file",
                }
            )
        return ToolResult(
            {
                "path": context.workspace.relative(path) if path != context.workspace.root else ".",
                "entries": entries,
                "truncated": truncated,
            }
        )


class GlobTool(_ReadOnlyTool):
    spec = ToolSpec(
        name="glob",
        description="Find workspace paths by a relative glob pattern before reading content.",
        input_schema={
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 1000},
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "matches": {"type": "array", "items": {"type": "string"}},
                "truncated": {"type": "boolean"},
            },
            "required": ["pattern", "matches", "truncated"],
            "additionalProperties": False,
        },
        risk=RiskLevel.LOW,
        max_result_size_chars=12_000,
        search_hint="locate candidate files before grep",
    )

    def validate(self, arguments: JSONObject) -> JSONObject:
        _reject_unknown(arguments, {"pattern", "max_results"})
        return {
            "pattern": _string(arguments, "pattern"),
            "max_results": _integer(arguments, "max_results", default=200, maximum=1000),
        }

    def _permission_resource(self, context: ToolContext, arguments: JSONObject) -> str:
        return context.workspace.validate_pattern(_string(arguments, "pattern"))

    async def execute(
        self,
        context: ToolContext,
        arguments: JSONObject,
        cancellation: CancellationToken,
    ) -> ToolResult:
        pattern = context.workspace.validate_pattern(_string(arguments, "pattern"))
        maximum = _integer(arguments, "max_results", default=200, maximum=1000)
        matches: list[str] = []
        truncated = False
        for candidate in context.workspace.root.glob(pattern):
            cancellation.raise_if_cancelled()
            canonical = context.workspace.resolve(candidate)
            if len(matches) >= maximum:
                truncated = True
                break
            matches.append(context.workspace.relative(canonical))
        return ToolResult({"pattern": pattern, "matches": sorted(matches), "truncated": truncated})


class GrepTool(_ReadOnlyTool):
    spec = ToolSpec(
        name="grep",
        description="Search matching lines in selected workspace text files.",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "glob": {"type": "string", "default": "**/*"},
                "regex": {"type": "boolean", "default": False},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 500},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "glob": {"type": "string"},
                "matches": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "line": {"type": "integer"},
                            "text": {"type": "string"},
                        },
                        "required": ["path", "line", "text"],
                        "additionalProperties": False,
                    },
                },
                "truncated": {"type": "boolean"},
            },
            "required": ["query", "matches", "truncated"],
            "additionalProperties": False,
        },
        risk=RiskLevel.LOW,
        max_result_size_chars=16_000,
        search_hint="find relevant lines before read_range",
    )

    def validate(self, arguments: JSONObject) -> JSONObject:
        _reject_unknown(arguments, {"query", "glob", "regex", "max_results"})
        query = _string(arguments, "query")
        pattern = _string(arguments, "glob", default="**/*")
        regex = arguments.get("regex", False)
        if not isinstance(regex, bool):
            raise ToolValidationError("regex must be a boolean")
        if regex:
            try:
                re.compile(query)
            except re.error as exc:
                raise ToolValidationError(f"Invalid regular expression: {exc}") from exc
        return {
            "query": query,
            "glob": pattern,
            "regex": regex,
            "max_results": _integer(arguments, "max_results", default=100, maximum=500),
        }

    def _permission_resource(self, context: ToolContext, arguments: JSONObject) -> str:
        return context.workspace.validate_pattern(_string(arguments, "glob", default="**/*"))

    async def execute(
        self,
        context: ToolContext,
        arguments: JSONObject,
        cancellation: CancellationToken,
    ) -> ToolResult:
        query = _string(arguments, "query")
        glob_pattern = context.workspace.validate_pattern(
            _string(arguments, "glob", default="**/*")
        )
        use_regex = arguments.get("regex", False) is True
        matcher = re.compile(query) if use_regex else None
        maximum = _integer(arguments, "max_results", default=100, maximum=500)
        matches: list[JSONObject] = []
        truncated = False
        for candidate in context.workspace.root.glob(glob_pattern):
            cancellation.raise_if_cancelled()
            path = context.workspace.resolve(candidate)
            if not path.is_file() or path.stat().st_size > 2_000_000:
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeError):
                continue
            for line_number, line in enumerate(lines, start=1):
                cancellation.raise_if_cancelled()
                found = bool(matcher.search(line)) if matcher else query in line
                if not found:
                    continue
                if len(matches) >= maximum:
                    truncated = True
                    break
                matches.append(
                    {
                        "path": context.workspace.relative(path),
                        "line": line_number,
                        "text": line,
                    }
                )
            if truncated:
                break
        return ToolResult(
            {
                "query": query,
                "glob": glob_pattern,
                "matches": matches,
                "truncated": truncated,
            }
        )

    def project(self, result: ToolResult) -> ToolProjection:
        """`fichero:linea: texto`, que es como se leen las coincidencias en cualquier
        herramienta de busqueda y como el modelo puede encadenarlas con `read_range`."""
        salida = result.output if isinstance(result.output, dict) else {}
        crudas = salida.get("matches")
        lineas = [
            f"{item.get('path')}:{item.get('line')}: {item.get('text')}"
            for item in (crudas if isinstance(crudas, list) else [])
            if isinstance(item, dict)
        ]
        return _listing(
            self.spec.name,
            lineas,
            title=str(salida.get("query", self.spec.name)),
            facts={
                "query": salida.get("query"),
                "count": len(lineas),
                "truncated": bool(salida.get("truncated")),
            },
            singular="coincidencia",
            plural="coincidencias",
        )


def _relative_path(result: ToolResult) -> str:
    salida = result.output if isinstance(result.output, dict) else {}
    ruta = salida.get("path")
    return ruta if isinstance(ruta, str) else ""


def _numbered(name: str, result: ToolResult, title: str) -> ToolProjection:
    salida = result.output if isinstance(result.output, dict) else {}
    crudas = salida.get("lines")
    lineas = [
        f"{item.get('line')}: {item.get('text')}"
        for item in (crudas if isinstance(crudas, list) else [])
        if isinstance(item, dict)
    ]
    return _listing(
        name,
        lineas,
        title=title or name,
        facts={
            "path": salida.get("path"),
            "start_line": salida.get("start_line"),
            "end_line": salida.get("end_line"),
            "count": len(lineas),
        },
        singular="linea",
        plural="lineas",
    )


def _listing(
    name: str, lineas: list[str], *, title: str, facts: JSONObject, singular: str, plural: str
) -> ToolProjection:
    cuerpo = "\n".join(lineas)
    recortado = len(cuerpo) > MODEL_TEXT_LIMIT
    if recortado:
        cuerpo = cuerpo[:MODEL_TEXT_LIMIT] + "\n[…recortado]"
    return ToolProjection(
        model=ModelView(cuerpo or "(sin resultados)", truncated=recortado),
        display=DisplayView(
            kind=ResultKind.ITEMS,
            title=title,
            summary=f"{len(lineas)} " + (singular if len(lineas) == 1 else plural),
            items=tuple(lineas[:DISPLAY_ITEM_LIMIT]),
            facts=facts,
        ),
    )


def repository_read_tools() -> tuple[Tool, ...]:
    return (
        GlobTool(),
        GrepTool(),
        ReadRangeTool(),
        ReadFileTool(),
        ListDirectoryTool(),
    )
