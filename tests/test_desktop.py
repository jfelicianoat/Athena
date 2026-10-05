from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from pathlib import Path

import pytest

from athena.adapters.ai_broker import AiBrokerModelProvider
from athena.adapters.openai_compatible import OpenAICompatibleModelProvider
from athena.cancellation import CancellationToken
from athena.events import InMemoryEventBus
from athena.types import JSONObject, JSONValue
from athena_desktop.config import (
    DesktopSettings,
    ProviderKind,
    SettingsStore,
    default_settings_path,
    resolve_token,
)
from athena_desktop.runtime import (
    DesktopStores,
    RunConfiguration,
    build_provider,
    build_registry,
    check_connection,
    requires_workspace_change,
    run_options,
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


@pytest.mark.parametrize(
    "status,payload,expected,message",
    [
        (200, {"authenticated": True, "auth_required": True}, True, "acepta el token"),
        (200, {"authenticated": True, "auth_required": False}, True, "no exige credenciales"),
        (403, {}, False, "rechaza el token"),
        (503, {}, False, "autenticación"),
    ],
)
def test_connection_reports_broker_authentication_without_submitting_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    payload: JSONObject,
    expected: bool,
    message: str,
) -> None:
    paths: list[str] = []

    class ConnectionBroker(AiBrokerModelProvider):
        async def _call(
            self,
            method: str,
            path: str,
            body: Mapping[str, JSONValue] | None,
            cancellation: CancellationToken | None,
        ) -> tuple[int, JSONObject]:
            assert method == "GET" and body is None
            assert cancellation is not None
            cancellation.raise_if_cancelled()
            paths.append(path)
            if path == "/health":
                return 200, {"status": "healthy"}
            assert path == "/api/v1/auth/check"
            return status, payload

    def provider(configuration: RunConfiguration) -> AiBrokerModelProvider:
        return ConnectionBroker(configuration.base_url, configuration.token)

    monkeypatch.setattr("athena_desktop.runtime.build_provider", provider)
    result = asyncio.run(check_connection(_configuration(tmp_path)))
    assert result.ok is expected
    assert message in result.message
    assert paths == ["/health", "/api/v1/auth/check"]


def test_broker_requires_a_token_before_starting(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="token"):
        build_provider(_configuration(tmp_path, token=""))


def test_broker_allows_athena_capabilities_through_its_adapter(tmp_path: Path) -> None:
    configuration = _configuration(tmp_path, writes="ask")

    configuration.validate()


def test_each_task_kind_maps_to_its_profile_and_evidence(tmp_path: Path) -> None:
    question = run_options(_configuration(tmp_path, writes="allow"))
    change = run_options(_configuration(tmp_path, writes="ask", task_kind="change"))
    documents = run_options(
        _configuration(tmp_path, writes="ask", task_kind="documents", deliverables=("a.md",))
    )

    assert (question.profile, question.writes.value, question.require_change) == (
        "questions",
        "off",
        False,
    )
    assert (change.profile, change.require_change) == ("software_engineering", True)
    assert (documents.profile, documents.deliverables) == ("documents", ("a.md",))


def test_desktop_recognises_an_explicit_file_change_objective() -> None:
    assert requires_workspace_change(
        "Escribe un fichero con las impresiones que te da un cuadro contemporáneo"
    )
    assert requires_workspace_change("Crae un archivo con mis impresiones")
    assert not requires_workspace_change("Explica qué impresiones te da el cuadro")


def test_desktop_registers_only_explicitly_enabled_capabilities(tmp_path: Path) -> None:
    registry = build_registry(_configuration(tmp_path), DesktopStores(tmp_path / "state"))
    bus = InMemoryEventBus()
    read_only = registry.tools_for(run_options(_configuration(tmp_path)), bus)
    enabled = registry.tools_for(
        run_options(_configuration(tmp_path, writes="ask", execution="ask", task_kind="change")),
        bus,
    )

    read_names = {tool.spec.name for tool in read_only}
    enabled_names = {tool.spec.name for tool in enabled}
    assert {"write_file", "edit_file", "bash", "git_commit"}.isdisjoint(read_names)
    assert {"write_file", "edit_file", "bash", "git_commit"} <= enabled_names
