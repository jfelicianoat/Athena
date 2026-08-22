"""Capability-based tool contracts; no concrete external tools live here."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

from athena.cancellation import CancellationToken
from athena.permissions import PermissionRequest, RiskLevel
from athena.types import JSONObject, JSONSchema, JSONValue
from athena.workspace import Workspace


class ToolLoadPolicy(StrEnum):
    CORE = "core"
    DEFERRED = "deferred"


class ToolResultSizePolicy(StrEnum):
    INLINE_OR_EXTERNALIZE = "inline_or_externalize"
    ALWAYS_EXTERNALIZE = "always_externalize"


class OutputContract(StrEnum):
    """Si `output_schema` obliga o solo describe.

    Obliga por defecto. Un esquema declarado y nunca comprobado es documentacion que se
    desincroniza del codigo sin que nada lo denuncie, y cuanto mas se confia en el peor:
    quien proyecta un resultado, quien lo guarda y quien lo ensena dan por ciertos unos
    campos que puede que ya no esten.
    """

    #: El resultado debe cumplirlo. Si no, la llamada falla y se dice por que.
    ENFORCED = "enforced"
    #: El esquema describe lo que se espera, pero Athena no puede responder por quien lo
    #: produce —una tool remota, por ejemplo—. La desviacion se publica, no se impone.
    DECLARED = "declared"


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    input_schema: JSONSchema
    output_schema: JSONSchema
    risk: RiskLevel
    max_result_size_chars: int
    load_policy: ToolLoadPolicy = ToolLoadPolicy.CORE
    result_size_policy: ToolResultSizePolicy = ToolResultSizePolicy.INLINE_OR_EXTERNALIZE
    output_contract: OutputContract = OutputContract.ENFORCED
    search_hint: str | None = None

    def __post_init__(self) -> None:
        if not self.name or not self.description:
            raise ValueError("Tool name and description must be non-empty")
        if self.max_result_size_chars <= 0:
            raise ValueError("max_result_size_chars must be positive")


@dataclass(frozen=True, slots=True)
class ToolContext:
    session_id: str
    workspace: Workspace
    call_id: str
    metadata: JSONObject = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ToolResultReference:
    store_key: str
    media_type: str
    size_chars: int
    checksum: str | None = None

    @property
    def uri(self) -> str:
        return f"athena-result://{self.store_key}"


@dataclass(frozen=True, slots=True)
class ToolResult:
    output: JSONValue
    call_id: str | None = None
    reference: ToolResultReference | None = None
    metadata: JSONObject = field(default_factory=dict)


@runtime_checkable
class Tool(Protocol):
    @property
    def spec(self) -> ToolSpec: ...

    def validate(self, arguments: JSONObject) -> JSONObject: ...

    def permission(self, context: ToolContext, arguments: JSONObject) -> PermissionRequest: ...

    async def execute(
        self,
        context: ToolContext,
        arguments: JSONObject,
        cancellation: CancellationToken,
    ) -> ToolResult: ...

    def is_read_only(self, arguments: JSONObject) -> bool: ...

    def is_destructive(self, arguments: JSONObject) -> bool: ...

    def is_concurrency_safe(self, arguments: JSONObject) -> bool: ...
