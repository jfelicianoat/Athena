"""Non-secret desktop preferences.

Credentials intentionally do not belong to this model. The UI keeps them in memory for
the current process and can read them from environment variables supplied by a secret
manager or launcher.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Literal, cast

CapabilityMode = Literal["off", "ask", "allow"]
_CAPABILITY_MODES = frozenset({"off", "ask", "allow"})
_TASK_KINDS = frozenset({"question", "change", "documents"})

#: Donde suele estar cada proveedor. Cambiar de proveedor recupera la URL que se uso con
#: el la ultima vez en vez de dejar la del otro (A03).
DEFAULT_URLS = {
    "ai_broker": "http://localhost:8000",
    "openai_compatible": "http://localhost:1234/v1",
}


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
    #: Un turno de un modelo local via broker puede tardar varios minutos; 120 s cortaba
    #: el primer turno de casi cualquier run real.
    timeout_seconds: float = 900.0
    task_kind: str = "question"
    deliverables: str = ""
    #: La URL y el modelo usados por ultima vez con cada proveedor.
    provider_urls: dict[str, str] = field(default_factory=dict)
    provider_models: dict[str, str] = field(default_factory=dict)

    def url_for(self, provider: ProviderKind) -> str:
        remembered = self.provider_urls.get(provider.value)
        if remembered:
            return remembered
        # Ajustes de antes de recordar por proveedor: lo guardado era del activo.
        if provider is self.provider and self.base_url:
            return self.base_url
        return DEFAULT_URLS[provider.value]

    def model_for(self, provider: ProviderKind) -> str:
        if provider.value in self.provider_models:
            return self.provider_models[provider.value]
        return self.model if provider is self.provider else ""

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
        base_url = _text(value.get("base_url"), DEFAULT_URLS[provider.value])
        model = _text(value.get("model"), "")
        urls = _text_map(value.get("provider_urls"))
        models = _text_map(value.get("provider_models"))
        task_kind = _text(value.get("task_kind"), "question")
        return cls(
            provider=provider,
            base_url=base_url,
            model=model,
            workspace=_text(value.get("workspace"), ""),
            writes=writes,
            execution=execution,
            max_iterations=_positive_int(value.get("max_iterations"), 12),
            timeout_seconds=_positive_float(value.get("timeout_seconds"), 900.0),
            task_kind=task_kind if task_kind in _TASK_KINDS else "question",
            deliverables=_text(value.get("deliverables"), ""),
            provider_urls=urls,
            provider_models=models,
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
    return environment_token(provider, environment)


def environment_token(provider: ProviderKind, environment: dict[str, str] | None = None) -> str:
    """La credencial que el entorno da para *este* proveedor, y solo para el."""
    env = os.environ if environment is None else environment
    variable = "ATHENA_BROKER_TOKEN" if provider is ProviderKind.AI_BROKER else "ATHENA_API_KEY"
    return env.get(variable, "").strip()


def _text(value: object, default: str) -> str:
    return value if isinstance(value, str) else default


def _text_map(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key): item
        for key, item in value.items()
        if str(key) in DEFAULT_URLS and isinstance(item, str)
    }


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
    "DEFAULT_URLS",
    "CapabilityMode",
    "DesktopSettings",
    "ProviderKind",
    "SettingsStore",
    "default_settings_path",
    "environment_token",
    "resolve_token",
]
