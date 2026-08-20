"""Wire format.

Everything the service sends is a projection of runtime state, never the runtime objects
themselves. Two reasons: a client must not depend on Athena's internal shapes, and a
projection is the natural place to be explicit about what crosses the boundary.

Event payloads are already redacted by the event bus before publication, so this module
does not redact again — it maps.
"""

from __future__ import annotations

from typing import Any

from athena.events import RuntimeEvent
from athena.session_store import SessionRecord
from athena.state import AgentStatus
from athena.types import JSONObject

#: Bumped when a projection changes shape in a way a client would notice.
WIRE_VERSION = 1


def event_to_json(event: RuntimeEvent) -> JSONObject:
    return {
        "event_id": event.event_id,
        "name": event.name.value,
        "run_id": event.session_id,
        "correlation_id": event.correlation_id,
        "occurred_at": event.occurred_at.isoformat(),
        "payload": dict(event.payload),
    }


def session_to_json(record: SessionRecord) -> JSONObject:
    """The snapshot a client receives on connect, and again on reconnect."""
    memory = record.working_memory
    return {
        "run_id": record.session_id,
        "workspace_id": record.workspace_id,
        "status": record.status.value,
        "resumable": record.resumable,
        "degraded": record.degraded,
        "objective": memory.objective,
        "created_at": record.created_at.isoformat(),
        "updated_at": record.updated_at.isoformat(),
        "working_memory": memory.to_json(),
        "verification": dict(record.verification),
        "tool_references": [
            {
                "uri": reference.uri,
                "store_key": reference.store_key,
                "media_type": reference.media_type,
                "size_chars": reference.size_chars,
            }
            for reference in record.tool_references
        ],
        "checkpoints": [
            {
                "name": checkpoint.name,
                "occurred_at": checkpoint.occurred_at.isoformat(),
                "payload": dict(checkpoint.payload),
            }
            for checkpoint in record.checkpoints
        ],
    }


def run_summary_to_json(record: SessionRecord) -> JSONObject:
    """The short form used by listings, where a full snapshot would be wasteful."""
    return {
        "run_id": record.session_id,
        "workspace_id": record.workspace_id,
        "status": record.status.value,
        "resumable": record.resumable,
        "degraded": record.degraded,
        "objective": record.working_memory.objective,
        "files_modified": list(record.working_memory.files_modified),
        "updated_at": record.updated_at.isoformat(),
    }


def status_from_json(value: str) -> AgentStatus | None:
    try:
        return AgentStatus(value)
    except ValueError:
        return None


def error_to_json(code: str, message: str, **extra: Any) -> JSONObject:
    payload: dict[str, Any] = {"error": {"code": code, "message": message}}
    if extra:
        payload["error"].update(extra)
    return payload


__all__ = [
    "WIRE_VERSION",
    "error_to_json",
    "event_to_json",
    "run_summary_to_json",
    "session_to_json",
    "status_from_json",
]
