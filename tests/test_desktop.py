from __future__ import annotations

import json
from pathlib import Path

import pytest

from athena.adapters.ai_broker import AiBrokerModelProvider
from athena.adapters.openai_compatible import OpenAICompatibleModelProvider
from athena.events import InMemoryEventBus
from athena.verification import LoopCompletionVerificationPolicy
from athena.workspace import Workspace
from athena_desktop.config import (
    DesktopSettings,
    ProviderKind,
    SettingsStore,
    default_settings_path,
    resolve_token,
)
from athena_desktop.runtime import (
    RunConfiguration,
    build_provider,
    build_tools,
    build_verification,
    requires_workspace_change,
)


def _configuration(tmp_path: Path, **overrides: object) -> RunConfiguration:
    values: dict[str, object] = {
        "workspace": tmp_path,
        "objective": "Explica este proyecto",
        "provider": ProviderKind.AI_BROKER,
        "base_url": "http://localhost:8000",
        "token": "broker-secret",
    }
    values.update(overrides)
    return RunConfiguration(**values)  # type: ignore[arg-type]


def test_settings_round_trip_without_a_secret(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    store = SettingsStore(path)
    settings = DesktopSettings(
        provider=ProviderKind.OPENAI_COMPATIBLE,
        base_url="http://localhost:1234/v1",
        model="local-model",
        workspace=str(tmp_path),
        writes="ask",
        execution="off",
        max_iterations=7,
        timeout_seconds=45,
    )

    store.save(settings)

    assert store.load() == settings
    raw = path.read_text(encoding="utf-8").lower()
    assert "token" not in raw
    assert "secret" not in raw


def test_invalid_settings_fall_back_to_safe_values(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps(
            {
                "provider": "unknown",
                "writes": "allow-everything",
                "execution": "yes",
                "max_iterations": -4,
            }
        ),
        encoding="utf-8",
    )

    settings = SettingsStore(path).load()

    assert settings.provider is ProviderKind.AI_BROKER
    assert settings.writes == "off"
    assert settings.execution == "off"
    assert settings.max_iterations == 12


def test_tokens_can_come_from_the_environment_without_being_persisted() -> None:
    environment = {
        "ATHENA_BROKER_TOKEN": " broker-token ",
        "ATHENA_API_KEY": " api-token ",
    }

    assert resolve_token(ProviderKind.AI_BROKER, "", environment) == "broker-token"
    assert resolve_token(ProviderKind.OPENAI_COMPATIBLE, "", environment) == "api-token"
    assert resolve_token(ProviderKind.AI_BROKER, "explicit", environment) == "explicit"


def test_default_settings_use_local_app_data() -> None:
    path = default_settings_path({"LOCALAPPDATA": r"C:\Users\test\AppData\Local"})

    assert path == Path(r"C:\Users\test\AppData\Local") / "Athena" / "settings.json"


def test_provider_selection_builds_the_requested_adapter(tmp_path: Path) -> None:
    broker = build_provider(_configuration(tmp_path))
    compatible = build_provider(
        _configuration(
            tmp_path,
            provider=ProviderKind.OPENAI_COMPATIBLE,
            base_url="http://localhost:1234/v1",
            model="local-model",
            token="api-secret",
        )
    )

    assert isinstance(broker, AiBrokerModelProvider)
    assert isinstance(compatible, OpenAICompatibleModelProvider)


def test_broker_requires_a_token_before_starting(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="token"):
        build_provider(_configuration(tmp_path, token=""))


def test_broker_allows_athena_capabilities_through_its_adapter(tmp_path: Path) -> None:
    configuration = _configuration(tmp_path, writes="ask")

    configuration.validate()


def test_desktop_uses_loop_completion_when_a_folder_defines_no_checks(tmp_path: Path) -> None:
    policy = build_verification(Workspace.from_path(tmp_path), InMemoryEventBus())

    assert isinstance(policy, LoopCompletionVerificationPolicy)


def test_desktop_recognises_an_explicit_file_change_objective() -> None:
    assert requires_workspace_change(
        "Escribe un fichero con las impresiones que te da un cuadro contemporáneo"
    )
    assert requires_workspace_change("Crae un archivo con mis impresiones")
    assert not requires_workspace_change("Explica qué impresiones te da el cuadro")


def test_desktop_registers_only_explicitly_enabled_capabilities(tmp_path: Path) -> None:
    event_bus = InMemoryEventBus()
    read_only = build_tools(_configuration(tmp_path), event_bus)
    enabled = build_tools(_configuration(tmp_path, writes="ask", execution="ask"), event_bus)

    read_names = {tool.spec.name for tool in read_only}
    enabled_names = {tool.spec.name for tool in enabled}
    assert {"write_file", "edit_file", "bash", "git_commit"}.isdisjoint(read_names)
    assert {"write_file", "edit_file", "bash", "git_commit"} <= enabled_names
