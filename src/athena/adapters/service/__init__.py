"""HTTP + SSE service adapter.

Outside Athena's core by construction: the runtime gains no transport, no framework and no
knowledge that a user interface exists. See ADR-017.
"""

from athena.adapters.service.approvals import (
    ApprovalAbandonedError,
    ApprovalRegistry,
    PendingApproval,
    RemotePermissionPrompt,
)
from athena.adapters.service.orchestration import ExecutionMode
from athena.adapters.service.projections import (
    WIRE_VERSION,
    event_to_json,
    run_summary_to_json,
    session_to_json,
)
from athena.adapters.service.runs import CapabilityMode, RunOptions, RunRegistry
from athena.adapters.service.server import AthenaService, ServiceConfig

__all__ = [
    "WIRE_VERSION",
    "ApprovalAbandonedError",
    "ApprovalRegistry",
    "AthenaService",
    "CapabilityMode",
    "ExecutionMode",
    "PendingApproval",
    "RemotePermissionPrompt",
    "RunOptions",
    "RunRegistry",
    "ServiceConfig",
    "event_to_json",
    "run_summary_to_json",
    "session_to_json",
]
