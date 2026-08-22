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


def _could_carry_a_secret(value: JSONValue) -> bool:
    """Whether a value is the sort of thing a credential can hide in.

    The key rule alone is too blunt: `input_tokens` matches `token`, and redacting a
    *count* of tokens taught the metrics that every run used zero — a plausible-looking
    number that was actually a redaction, which is worse than a missing field because
    nothing about it looks wrong.

    Numbers and booleans are let through because no credential is a bare integer. Strings
    and containers are still redacted on the key alone, which is where secrets do live and
    where being over-cautious costs nothing.
    """
    if isinstance(value, (bool, int, float)):
        return False
    return value is not None


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
            str(key): "[REDACTED]"
            if _SECRET_KEY.search(str(key)) and _could_carry_a_secret(item)
            else redact_sensitive(item)
            for key, item in value.items()
        }
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [redact_sensitive(item) for item in value]
    return value
