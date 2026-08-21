"""Non-secret desktop preferences.

Credentials intentionally do not belong to this model. The UI keeps them in memory for
the current process and can read them from environment variables supplied by a secret
manager or launcher.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal, cast

CapabilityMode = Literal["off", "ask", "allow"]
_CAPABILITY_MODES = frozenset({"off", "ask", "allow"})


class ProviderKind(StrEnum):
    OPENAI_COMPATIBLE = "openai_compatible"
    AI_BROKER = "ai_broker"


@dataclass(frozen=True, slots=True)
class DesktopSettings:
    provider: ProviderKind = ProviderKind.AI_BROKER
    base_url: str = "http://localhost:8000"
    model: str = ""
    workspace: str = ""
    writes: CapabilityMode = "off"
    execution: CapabilityMode = "off"
    max_iterations: int = 12
    timeout_seconds: float = 120.0

    @classmethod
    def from_json(cls, value: object) -> DesktopSettings:
        if not isinstance(value, dict):
            return cls()
        provider_value = value.get("provider", ProviderKind.AI_BROKER.value)
        try:
            provider = ProviderKind(str(provider_value))
        except ValueError:
            provider = ProviderKind.AI_BROKER
        writes = _capability(value.get("writes"))
        execution = _capability(value.get("execution"))
        return cls(
            provider=provider,
            base_url=_text(value.get("base_url"), "http://localhost:8000"),
            model=_text(value.get("model"), ""),
            workspace=_text(value.get("workspace"), ""),
            writes=writes,
            execution=execution,
            max_iterations=_positive_int(value.get("max_iterations"), 12),
            timeout_seconds=_positive_float(value.get("timeout_seconds"), 120.0),
        )


class SettingsStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_settings_path()

    def load(self) -> DesktopSettings:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return DesktopSettings()
        return DesktopSettings.from_json(raw)

    def save(self, settings: DesktopSettings) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = asdict(settings)
        payload["provider"] = settings.provider.value
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temporary.replace(self.path)


def default_settings_path(environment: dict[str, str] | None = None) -> Path:
    env = os.environ if environment is None else environment
    local = env.get("LOCALAPPDATA", "").strip()
    if local:
        return Path(local) / "Athena" / "settings.json"
    return Path.home() / ".athena" / "desktop-settings.json"


def resolve_token(
    provider: ProviderKind,
    supplied: str,
    environment: dict[str, str] | None = None,
) -> str:
    if supplied.strip():
        return supplied.strip()
    env = os.environ if environment is None else environment
    variable = "ATHENA_BROKER_TOKEN" if provider is ProviderKind.AI_BROKER else "ATHENA_API_KEY"
    return env.get(variable, "").strip()


def _text(value: object, default: str) -> str:
    return value if isinstance(value, str) else default


def _capability(value: object) -> CapabilityMode:
    candidate = str(value)
    if candidate in _CAPABILITY_MODES:
        return cast(CapabilityMode, candidate)
    return "off"


def _positive_int(value: object, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return default
    return value


def _positive_float(value: object, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return default
    return float(value)


__all__ = [
    "CapabilityMode",
    "DesktopSettings",
    "ProviderKind",
    "SettingsStore",
    "default_settings_path",
    "resolve_token",
]
