from __future__ import annotations

import asyncio
from pathlib import Path

from athena.cancellation import CancellationSource
from athena.cli import _parser, _run


def test_cli_propagates_cancellation_instead_of_running(tmp_path: Path) -> None:
    """A cancelled token must reach the loop before any provider call is attempted."""

    async def scenario() -> None:
        arguments = _parser().parse_args([str(tmp_path), "--objective", "Explain the layout"])
        source = CancellationSource()
        source.cancel()

        exit_code = await asyncio.wait_for(_run(arguments, source), timeout=5)

        assert exit_code == 130

    asyncio.run(scenario())


def test_cli_parser_defaults_keep_provider_configuration_outside_the_runtime(
    tmp_path: Path,
) -> None:
    arguments = _parser().parse_args([str(tmp_path)])

    assert arguments.workspace == tmp_path
    assert arguments.base_url
    assert arguments.model
    assert arguments.max_iterations > 0


def test_cli_registers_capability_tools_only_when_enabled(tmp_path: Path) -> None:
    from athena.cli import _tools
    from athena.events import InMemoryEventBus

    bus = InMemoryEventBus()
    read_only = _parser().parse_args([str(tmp_path)])
    full = _parser().parse_args([str(tmp_path), "--writes", "ask", "--exec", "ask"])

    read_names = {tool.spec.name for tool in _tools(read_only, bus)}
    full_names = {tool.spec.name for tool in _tools(full, bus)}

    assert "git_status" in read_names
    assert {"write_file", "edit_file", "bash", "git_commit"}.isdisjoint(read_names)
    assert {"write_file", "edit_file", "bash", "git_commit"} <= full_names
    assert "git_push" not in full_names


def test_cli_policy_only_grants_what_was_explicitly_allowed(tmp_path: Path) -> None:
    from athena.permissions import PermissionPolicy, PolicyPermissionEngine

    arguments = _parser().parse_args([str(tmp_path), "--writes", "allow"])
    policy = PermissionPolicy(
        allow_workspace_writes=arguments.writes == "allow",
        allow_local_execution=arguments.execution == "allow",
    )
    engine = PolicyPermissionEngine(policy)

    assert engine.policy.allow_workspace_writes is True
    assert engine.policy.allow_local_execution is False
