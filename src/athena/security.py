"""Deterministic redaction for observable logs and event payloads."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from athena.types import JSONValue

_SECRET_KEY = re.compile(r"(?:api[_-]?key|token|secret|password|authorization)", re.I)
_SECRET_TEXT = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*"),
    re.compile(r"(?i)(api[_-]?key|token|secret|password)\s*[:=]\s*([^\s,;]+)"),
)


def redact_text(value: str) -> str:
    redacted = value
    for pattern in _SECRET_TEXT:
        if pattern.groups:
            redacted = pattern.sub(lambda match: f"{match.group(1)}=[REDACTED]", redacted)
        else:
            redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def redact_sensitive(value: JSONValue) -> JSONValue:
    if isinstance(value, Mapping):
        return {
            str(key): "[REDACTED]" if _SECRET_KEY.search(str(key)) else redact_sensitive(item)
            for key, item in value.items()
        }
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [redact_sensitive(item) for item in value]
    return value
