"""MCP adapter boundary.

MCP is not part of Athena's core, and this module is the seam that keeps it that way. The
core knows `Tool`; it does not know that a tool came from a remote server, and it never
will. Everything crossing this boundary is wrapped so that an MCP tool is subject to the
same rules a native one is:

- its schema is validated and its unknown fields rejected;
- it gets a `PermissionRequest` with a tier, defaulting to R3 — an external server is not
  something to trust by default;
- it runs under a mandatory timeout and honours cancellation;
- its output is bounded, and an oversized result is externalized like any other.

There is no transport here. `McpClient` is a Protocol; a real deployment supplies stdio or
HTTP, and tests supply a fake. Nothing in `athena.mcp` reaches the network by itself.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from athena.async_utils import await_cancellable
from athena.cancellation import CancellationToken
from athena.errors import AthenaRuntimeError, ToolExecutionError, ToolValidationError
from athena.permissions import PermissionRequest, RiskLevel, RiskTier
from athena.tools import (
    Tool,
    ToolContext,
    ToolLoadPolicy,
    ToolResult,
    ToolResultSizePolicy,
    ToolSpec,
)
from athena.types import JSONObject, JSONSchema

_DEFAULT_TIMEOUT_SECONDS = 60.0
_DEFAULT_RESULT_LIMIT = 16_000


@dataclass(frozen=True, slots=True)
class McpToolDescriptor:
    """What a server says about one of its tools."""

    name: str
    description: str
    input_schema: JSONSchema = field(default_factory=dict)
    #: Servers may advertise a read-only tool; it is a hint, never a grant.
    read_only: bool = False


@runtime_checkable
class McpClient(Protocol):
    server_name: str

    async def list_tools(self, cancellation: CancellationToken) -> Sequence[McpToolDescriptor]: ...

    async def call_tool(
        self, name: str, arguments: JSONObject, cancellation: CancellationToken
    ) -> JSONObject: ...


@dataclass(frozen=True, slots=True)
class McpToolPolicy:
    """How much an MCP server is trusted. Conservative by default, on purpose."""

    #: R3 means every call is an ASK. Lower it only for a server you actually control.
    tier: RiskTier = RiskTier.R3_EXTERNAL_OR_IRREVERSIBLE
    risk: RiskLevel = RiskLevel.HIGH
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS
    max_result_size_chars: int = _DEFAULT_RESULT_LIMIT
    load_policy: ToolLoadPolicy = ToolLoadPolicy.DEFERRED
    concurrency_safe: bool = False

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("MCP timeout_seconds must be positive")
        if self.max_result_size_chars <= 0:
            raise ValueError("MCP max_result_size_chars must be positive")


class McpTool:
    """One remote tool, wearing Athena's contract."""

    def __init__(
        self,
        client: McpClient,
        descriptor: McpToolDescriptor,
        policy: McpToolPolicy | None = None,
        *,
        name_prefix: str = "mcp",
    ) -> None:
        self.client = client
        self.descriptor = descriptor
        self.policy = policy or McpToolPolicy()
        schema = descriptor.input_schema or {"type": "object"}
        self.spec = ToolSpec(
            name=f"{name_prefix}__{client.server_name}__{descriptor.name}",
            description=f"[{client.server_name}] {descriptor.description}",
            input_schema=schema,
            output_schema={"type": "object"},
            risk=self.policy.risk,
            max_result_size_chars=self.policy.max_result_size_chars,
            load_policy=self.policy.load_policy,
            result_size_policy=ToolResultSizePolicy.INLINE_OR_EXTERNALIZE,
            search_hint=f"{descriptor.description} (via the {client.server_name} MCP server)",
        )

    # -- contract ---------------------------------------------------------

    def validate(self, arguments: JSONObject) -> JSONObject:
        """Enforce the declared schema locally, before anything leaves the process."""
        properties = self.spec.input_schema.get("properties")
        if isinstance(properties, Mapping):
            allowed = set(properties)
            unknown = set(arguments) - allowed
            if unknown and self.spec.input_schema.get("additionalProperties") is False:
                raise ToolValidationError(
                    f"Unknown input fields for {self.spec.name}: {', '.join(sorted(unknown))}"
                )
        required = self.spec.input_schema.get("required")
        if isinstance(required, Sequence) and not isinstance(required, (str, bytes)):
            missing = [
                field_name
                for field_name in required
                if isinstance(field_name, str) and field_name not in arguments
            ]
            if missing:
                raise ToolValidationError(
                    f"Missing required field(s) for {self.spec.name}: {', '.join(missing)}"
                )
        return dict(arguments)

    def is_read_only(self, arguments: JSONObject) -> bool:
        del arguments
        return self.descriptor.read_only

    def is_destructive(self, arguments: JSONObject) -> bool:
        del arguments
        return not self.descriptor.read_only

    def is_concurrency_safe(self, arguments: JSONObject) -> bool:
        del arguments
        return self.policy.concurrency_safe

    def permission(self, context: ToolContext, arguments: JSONObject) -> PermissionRequest:
        return PermissionRequest(
            tool_name=self.spec.name,
            operation=f"mcp:{self.client.server_name}:{self.descriptor.name}",
            action=f"call {self.descriptor.name} on the {self.client.server_name} MCP server",
            workspace=context.workspace,
            risk=self.policy.risk,
            tier=self.policy.tier,
            is_read_only=self.descriptor.read_only,
            is_destructive=not self.descriptor.read_only,
            is_concurrency_safe=self.policy.concurrency_safe,
            reason=(
                f"The agent requested an external capability provided by "
                f"{self.client.server_name}, outside Athena's own code."
            ),
            possible_effects=(
                f"Sends the given arguments to the {self.client.server_name} MCP server",
                "Any effect of that call happens outside this workspace",
            ),
            resources=(self.descriptor.name,),
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
        try:
            payload = await await_cancellable(
                self.client.call_tool(self.descriptor.name, arguments, cancellation),
                cancellation,
                timeout=self.policy.timeout_seconds,
            )
        except AthenaRuntimeError:
            # Athena's own taxonomy passes through untouched: a cancellation or a timeout
            # crossing this boundary must stay what it is, or the loop would report a
            # cancelled run as a tool failure.
            raise
        except Exception as exc:
            # A remote server, by contrast, is not trusted to raise anything meaningful.
            raise ToolExecutionError(
                f"MCP call failed on {self.client.server_name}: {type(exc).__name__}",
                details={"server": self.client.server_name, "tool": self.descriptor.name},
            ) from exc
        if not isinstance(payload, Mapping):
            raise ToolExecutionError(
                f"{self.spec.name} returned a non-object payload",
                details={"server": self.client.server_name},
            )
        return ToolResult({"server": self.client.server_name, **dict(payload)})


async def mcp_tools(
    client: McpClient,
    cancellation: CancellationToken,
    *,
    policy: McpToolPolicy | None = None,
    include: Iterable[str] | None = None,
) -> tuple[Tool, ...]:
    """Adapt a server's advertised tools. Nothing is registered implicitly."""
    descriptors = await client.list_tools(cancellation)
    allowed = set(include) if include is not None else None
    return tuple(
        McpTool(client, descriptor, policy)
        for descriptor in descriptors
        if allowed is None or descriptor.name in allowed
    )


__all__ = [
    "McpClient",
    "McpTool",
    "McpToolDescriptor",
    "McpToolPolicy",
    "mcp_tools",
]
