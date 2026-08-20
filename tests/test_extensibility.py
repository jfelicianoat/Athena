from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from athena.cancellation import CancellationSource, CancellationToken
from athena.errors import PermissionDeniedError, ToolExecutionError, ToolValidationError
from athena.events import EventName, InMemoryEventBus, RuntimeEvent
from athena.hooks import (
    Hook,
    HookContext,
    HookDecision,
    HookEvent,
    HookRegistry,
    HookResult,
)
from athena.mcp import McpClient, McpTool, McpToolDescriptor, McpToolPolicy, mcp_tools
from athena.models import ModelToolCall
from athena.mutation_tools import workspace_mutation_tools
from athena.permissions import (
    PermissionDecision,
    PermissionPolicy,
    PolicyPermissionEngine,
    RiskTier,
)
from athena.registry import ToolRegistry
from athena.repository_tools import repository_read_tools
from athena.skills import SkillManifest, SkillRegistry, render_skills
from athena.stores import InMemoryToolResultStore
from athena.testing import ScriptedPermissionPrompt
from athena.tool_executor import ToolExecutor
from athena.tool_search import TOOL_SEARCH_NAME, ToolSearchTool
from athena.tools import ToolContext, ToolLoadPolicy
from athena.types import JSONObject
from athena.workspace import Workspace


def _definition_names(definitions: Sequence[JSONObject]) -> set[str]:
    names: set[str] = set()
    for definition in definitions:
        function = definition.get("function")
        if isinstance(function, Mapping):
            name = function.get("name")
            if isinstance(name, str):
                names.add(name)
    return names


def _context(root: Path) -> ToolContext:
    return ToolContext("session", Workspace.from_path(root), "call-1")


# ------------------------------------------------------------------ hooks


def _recorder(log: list[str], name: str) -> Hook:
    def handler(context: HookContext) -> None:
        log.append(f"{name}:{context.event.value}")

    return Hook(name, HookEvent.PRE_TOOL_USE, handler)


def test_hooks_run_in_declared_order_then_registration_order() -> None:
    log: list[str] = []

    def make(name: str, order: int) -> Hook:
        def handler(context: HookContext) -> None:
            del context
            log.append(name)

        return Hook(name, HookEvent.PRE_TOOL_USE, handler, order=order)

    registry = HookRegistry((make("third", 30), make("first", 10), make("second", 20)))

    async def scenario() -> None:
        report = await registry.run(HookContext(HookEvent.PRE_TOOL_USE, "s"))

        assert log == ["first", "second", "third"]
        assert report.ran == ("first", "second", "third")
        assert not report.blocked

    asyncio.run(scenario())


def test_the_first_block_wins_and_later_hooks_do_not_run() -> None:
    log: list[str] = []

    def blocker(context: HookContext) -> HookResult:
        del context
        log.append("blocker")
        return HookResult(HookDecision.BLOCK, "not on my watch")

    def later(context: HookContext) -> None:
        del context
        log.append("later")

    registry = HookRegistry(
        (
            Hook("blocker", HookEvent.PRE_TOOL_USE, blocker, order=10),
            Hook("later", HookEvent.PRE_TOOL_USE, later, order=20),
        )
    )

    async def scenario() -> None:
        report = await registry.run(HookContext(HookEvent.PRE_TOOL_USE, "s"))

        assert report.blocked
        assert report.blocked_by == "blocker"
        assert "not on my watch" in report.reason
        assert log == ["blocker"]

    asyncio.run(scenario())


def test_a_failing_observational_hook_is_recorded_and_ignored() -> None:
    log: list[str] = []

    def broken(context: HookContext) -> None:
        del context
        raise RuntimeError("hook is buggy")

    def survivor(context: HookContext) -> None:
        del context
        log.append("survivor")

    registry = HookRegistry(
        (
            Hook("broken", HookEvent.POST_TOOL_USE, broken, order=10),
            Hook("survivor", HookEvent.POST_TOOL_USE, survivor, order=20),
        )
    )

    async def scenario() -> None:
        report = await registry.run(HookContext(HookEvent.POST_TOOL_USE, "s"))

        assert not report.blocked
        assert log == ["survivor"]
        assert report.failures[0][0] == "broken"
        assert "RuntimeError" in report.failures[0][1]

    asyncio.run(scenario())


def test_a_failing_blocking_hook_refuses_rather_than_waving_through() -> None:
    """A guard that crashes must fail closed; otherwise breaking it disables it."""

    def broken_guard(context: HookContext) -> HookResult:
        del context
        raise RuntimeError("guard exploded")

    registry = HookRegistry((Hook("guard", HookEvent.PRE_TOOL_USE, broken_guard, blocking=True),))

    async def scenario() -> None:
        report = await registry.run(HookContext(HookEvent.PRE_TOOL_USE, "s"))

        assert report.blocked
        assert report.blocked_by == "guard"
        assert "guard exploded" in report.reason

    asyncio.run(scenario())


def test_an_async_hook_is_awaited() -> None:
    async def handler(context: HookContext) -> HookResult:
        del context
        await asyncio.sleep(0)
        return HookResult(HookDecision.BLOCK, "async refusal")

    registry = HookRegistry((Hook("async", HookEvent.ON_ERROR, handler),))

    async def scenario() -> None:
        report = await registry.run(HookContext(HookEvent.ON_ERROR, "s"))

        assert report.blocked_by == "async"

    asyncio.run(scenario())


def test_a_hook_can_block_a_tool_call_but_never_unblock_one(tmp_path: Path) -> None:
    """The asymmetry that keeps PermissionEngine the only authority."""
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")

    async def scenario() -> None:
        bus = InMemoryEventBus()
        registry = ToolRegistry(repository_read_tools())

        # A hook that tries as hard as it can to approve everything.
        def eager(context: HookContext) -> HookResult:
            del context
            return HookResult(HookDecision.CONTINUE, "please allow this", notes=("allow!",))

        executor = ToolExecutor(
            registry,
            # Deny-everything policy: no hook may rescue a call from it.
            PolicyPermissionEngine(PermissionPolicy()),
            InMemoryToolResultStore(),
            bus,
            prompt=ScriptedPermissionPrompt((PermissionDecision.DENY,)),
            hooks=HookRegistry((Hook("eager", HookEvent.PRE_TOOL_USE, eager),)),
        )
        workspace = Workspace.from_path(tmp_path)

        # A read is R0 and is allowed by the engine, hook or no hook.
        result = await executor.execute(
            ModelToolCall("r1", "read_file", {"path": "a.txt"}),
            session_id="s",
            workspace=workspace,
            cancellation=CancellationSource().token,
        )
        assert isinstance(result.output, dict)

        # A write is R1; the engine asks, the prompt refuses, and the hook cannot help.
        registry.register(workspace_mutation_tools(bus)[1])
        with pytest.raises(PermissionDeniedError):
            await executor.execute(
                ModelToolCall("w1", "write_file", {"path": "new.txt", "content": "x"}),
                session_id="s",
                workspace=workspace,
                cancellation=CancellationSource().token,
            )
        assert not (tmp_path / "new.txt").exists()

    asyncio.run(scenario())


def test_a_pre_edit_hook_can_veto_a_write(tmp_path: Path) -> None:
    async def scenario() -> None:
        bus = InMemoryEventBus()
        seen: list[str] = []

        def guard(context: HookContext) -> HookResult:
            seen.append(context.event.value)
            return HookResult(HookDecision.BLOCK, "this file is protected")

        executor = ToolExecutor(
            ToolRegistry(workspace_mutation_tools(bus)),
            PolicyPermissionEngine(PermissionPolicy(allow_workspace_writes=True)),
            InMemoryToolResultStore(),
            bus,
            hooks=HookRegistry((Hook("protect", HookEvent.PRE_EDIT, guard, blocking=True),)),
        )

        from athena.hooks import HookBlockedError

        with pytest.raises(HookBlockedError):
            await executor.execute(
                ModelToolCall("w1", "write_file", {"path": "new.txt", "content": "x"}),
                session_id="s",
                workspace=Workspace.from_path(tmp_path),
                cancellation=CancellationSource().token,
            )

        assert seen == [HookEvent.PRE_EDIT.value]
        assert not (tmp_path / "new.txt").exists()

    asyncio.run(scenario())


def test_edit_hooks_do_not_fire_for_a_read(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")

    async def scenario() -> None:
        bus = InMemoryEventBus()
        fired: list[str] = []

        def record(context: HookContext) -> None:
            fired.append(context.event.value)

        hooks = HookRegistry(
            (
                Hook("pre-edit", HookEvent.PRE_EDIT, record),
                Hook("post-edit", HookEvent.POST_EDIT, record),
                Hook("pre-tool", HookEvent.PRE_TOOL_USE, record),
                Hook("post-tool", HookEvent.POST_TOOL_USE, record),
            )
        )
        executor = ToolExecutor(
            ToolRegistry(repository_read_tools()),
            PolicyPermissionEngine(),
            InMemoryToolResultStore(),
            bus,
            hooks=hooks,
        )

        await executor.execute(
            ModelToolCall("r1", "read_file", {"path": "a.txt"}),
            session_id="s",
            workspace=Workspace.from_path(tmp_path),
            cancellation=CancellationSource().token,
        )

        assert fired == [HookEvent.PRE_TOOL_USE.value, HookEvent.POST_TOOL_USE.value]

    asyncio.run(scenario())


def test_on_error_fires_for_a_typed_tool_failure(tmp_path: Path) -> None:
    async def scenario() -> None:
        bus = InMemoryEventBus()
        errors: list[JSONObject] = []

        def record(context: HookContext) -> None:
            errors.append(dict(context.payload))

        executor = ToolExecutor(
            ToolRegistry(repository_read_tools()),
            PolicyPermissionEngine(),
            InMemoryToolResultStore(),
            bus,
            hooks=HookRegistry((Hook("errors", HookEvent.ON_ERROR, record),)),
        )

        with pytest.raises(ToolValidationError):
            await executor.execute(
                ModelToolCall("x1", "does_not_exist", {}),
                session_id="s",
                workspace=Workspace.from_path(tmp_path),
                cancellation=CancellationSource().token,
            )

        assert errors and errors[0]["error_code"] == "tool_validation_error"

    asyncio.run(scenario())


# ------------------------------------------------------------------ skills


def _skill(name: str, tasks: tuple[str, ...], toolsets: tuple[str, ...] = ()) -> SkillManifest:
    return SkillManifest(
        name=name,
        description=f"How to {name}",
        version="1.0.0",
        applicable_tasks=tasks,
        required_toolsets=toolsets,
        instructions=f"Procedure for {name}.",
        metadata={"author": "test"},
    )


def test_skills_are_selected_by_relevance() -> None:
    registry = SkillRegistry(
        (
            _skill("migrate-database", ("database migration", "schema change")),
            _skill("write-docs", ("documentation", "readme")),
        )
    )

    selected = registry.select("Apply the database migration for the new schema", ())

    assert [item.skill.name for item in selected] == ["migrate-database"]
    assert selected[0].score > 0


def test_an_irrelevant_task_selects_nothing() -> None:
    registry = SkillRegistry((_skill("migrate-database", ("database migration",)),))

    assert registry.select("Rename a CSS variable", ()) == ()


def test_a_skill_whose_toolsets_are_missing_is_dropped_not_accommodated() -> None:
    """A skill cannot cause a tool to exist; that would be silent escalation."""
    registry = SkillRegistry((_skill("deploy", ("deploy the service",), toolsets=("kubectl",)),))

    assert registry.select("deploy the service", ("read_file", "grep")) == ()
    dropped = registry.unavailable("deploy the service", ("read_file", "grep"))
    assert [skill.name for skill in dropped] == ["deploy"]


def test_selecting_a_skill_changes_neither_the_registry_nor_the_policy(
    tmp_path: Path,
) -> None:
    tools = ToolRegistry(repository_read_tools())
    before_tools = tools.names()
    engine = PolicyPermissionEngine(PermissionPolicy())
    before_policy = (
        engine.policy.allow_workspace_writes,
        engine.policy.allow_local_execution,
    )
    skills = SkillRegistry((_skill("edit-code", ("edit code", "fix bug")),))

    selected = skills.select("fix bug in the parser", tools.names())

    assert selected, "the skill should have been selected"
    assert tools.names() == before_tools
    assert (
        engine.policy.allow_workspace_writes,
        engine.policy.allow_local_execution,
    ) == before_policy
    assert "write_file" not in tools.names()
    assert tmp_path.exists()


def test_a_skill_manifest_requires_its_identifying_fields() -> None:
    with pytest.raises(ToolValidationError):
        SkillManifest(name="", description="d", version="1")
    with pytest.raises(ToolValidationError):
        SkillManifest(name="n", description="", version="1")
    with pytest.raises(ToolValidationError):
        SkillManifest(name="n", description="d", version="")


def test_rendered_skills_carry_their_instructions() -> None:
    registry = SkillRegistry((_skill("migrate-database", ("database migration",)),))

    rendered = render_skills(registry.select("database migration", ()))

    assert "migrate-database v1.0.0" in rendered
    assert "Procedure for migrate-database." in rendered


# ------------------------------------------------------------------ deferred tools


class _DeferredEcho:
    def __init__(self, name: str, description: str, hint: str) -> None:
        from athena.permissions import RiskLevel
        from athena.tools import ToolSpec

        self.spec = ToolSpec(
            name=name,
            description=description,
            input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
            output_schema={"type": "object"},
            risk=RiskLevel.LOW,
            max_result_size_chars=1_000,
            load_policy=ToolLoadPolicy.DEFERRED,
            search_hint=hint,
        )

    def validate(self, arguments: JSONObject) -> JSONObject:
        return arguments

    def permission(self, context: ToolContext, arguments: JSONObject):  # type: ignore[no-untyped-def]
        from athena.permissions import PermissionRequest, RiskLevel

        return PermissionRequest(
            self.spec.name,
            self.spec.name,
            context.workspace,
            RiskLevel.LOW,
            RiskTier.R0_READ_ONLY,
            True,
            False,
            arguments=arguments,
        )

    async def execute(self, context, arguments, cancellation):  # type: ignore[no-untyped-def]
        del context, cancellation
        from athena.tools import ToolResult

        return ToolResult(dict(arguments))

    def is_read_only(self, arguments: JSONObject) -> bool:
        return True

    def is_destructive(self, arguments: JSONObject) -> bool:
        return False

    def is_concurrency_safe(self, arguments: JSONObject) -> bool:
        return True


def _deferred_registry() -> ToolRegistry:
    registry = ToolRegistry(
        (
            *repository_read_tools(),
            _DeferredEcho("jira_issue", "Read a Jira issue", "look up a ticket by key"),
            _DeferredEcho("oracle_query", "Query Oracle", "run a read-only SQL query"),
        )
    )
    registry.register(ToolSearchTool(registry))
    return registry


def test_deferred_tools_are_absent_from_the_default_schema_list() -> None:
    registry = _deferred_registry()

    names = _definition_names(registry.definitions())

    assert "read_file" in names
    assert TOOL_SEARCH_NAME in names
    assert "jira_issue" not in names
    assert "oracle_query" not in names
    assert set(registry.deferred_names()) == {"jira_issue", "oracle_query"}


def test_search_finds_a_deferred_tool_and_revealing_it_adds_its_schema() -> None:
    registry = _deferred_registry()

    matches = registry.search("jira ticket")

    assert [tool.spec.name for tool in matches] == ["jira_issue"]
    revealed = _definition_names(registry.definitions({"jira_issue"}))
    assert "jira_issue" in revealed
    assert "oracle_query" not in revealed


def test_search_never_returns_a_core_tool() -> None:
    registry = _deferred_registry()

    assert registry.search("read a file") == () or all(
        tool.spec.load_policy is ToolLoadPolicy.DEFERRED for tool in registry.search("read a file")
    )


def test_the_search_tool_reports_matches_with_their_schemas(tmp_path: Path) -> None:
    async def scenario() -> None:
        registry = _deferred_registry()
        tool = ToolSearchTool(registry)

        result = await tool.execute(
            _context(tmp_path), {"query": "sql query"}, CancellationSource().token
        )

        assert isinstance(result.output, dict)
        assert result.output["revealed"] == ["oracle_query"]
        matches = result.output["matches"]
        assert isinstance(matches, list)
        first = matches[0]
        assert isinstance(first, Mapping)
        schema = first["input_schema"]
        assert isinstance(schema, Mapping)
        assert schema["type"] == "object"

    asyncio.run(scenario())


# ------------------------------------------------------------------ MCP


class _FakeMcpClient:
    """A stand-in server. No transport, no network, entirely under test control."""

    server_name = "fixture"

    def __init__(self, payload: JSONObject | None = None, *, fail: bool = False) -> None:
        self.payload = payload or {"ok": True}
        self.fail = fail
        self.calls: list[tuple[str, JSONObject]] = []
        self.started = asyncio.Event()
        self.hang = False

    async def list_tools(self, cancellation: CancellationToken) -> Sequence[McpToolDescriptor]:
        cancellation.raise_if_cancelled()
        return (
            McpToolDescriptor(
                "search_issues",
                "Search the issue tracker",
                {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                    "additionalProperties": False,
                },
                read_only=True,
            ),
            McpToolDescriptor("create_issue", "Create an issue", {"type": "object"}),
        )

    async def call_tool(
        self, name: str, arguments: JSONObject, cancellation: CancellationToken
    ) -> JSONObject:
        cancellation.raise_if_cancelled()
        self.calls.append((name, dict(arguments)))
        if self.fail:
            raise RuntimeError("the server exploded")
        if self.hang:
            self.started.set()
            await asyncio.Event().wait()
        return self.payload


def test_mcp_tools_are_adapted_into_the_native_contract() -> None:
    async def scenario() -> None:
        client = _FakeMcpClient()
        assert isinstance(client, McpClient)

        tools = await mcp_tools(client, CancellationSource().token)

        assert [tool.spec.name for tool in tools] == [
            "mcp__fixture__search_issues",
            "mcp__fixture__create_issue",
        ]
        assert all(tool.spec.load_policy is ToolLoadPolicy.DEFERRED for tool in tools)

    asyncio.run(scenario())


def test_an_mcp_tool_defaults_to_asking_every_time(tmp_path: Path) -> None:
    """An external server is not trusted by default, whatever it says about itself."""

    async def scenario() -> None:
        tools = await mcp_tools(_FakeMcpClient(), CancellationSource().token)
        engine = PolicyPermissionEngine(
            PermissionPolicy(allow_workspace_writes=True, allow_local_execution=True)
        )

        for tool in tools:
            request = tool.permission(_context(tmp_path), {"query": "x"})
            assert request.tier is RiskTier.R3_EXTERNAL_OR_IRREVERSIBLE
            assert engine.decide(request) is PermissionDecision.ASK

    asyncio.run(scenario())


def test_an_mcp_schema_is_enforced_before_anything_leaves_the_process() -> None:
    async def scenario() -> None:
        client = _FakeMcpClient()
        search = (await mcp_tools(client, CancellationSource().token))[0]

        with pytest.raises(ToolValidationError):
            search.validate({"wrong_field": 1})
        with pytest.raises(ToolValidationError):
            search.validate({})
        assert search.validate({"query": "ok"}) == {"query": "ok"}
        assert client.calls == [], "validation must not reach the server"

    asyncio.run(scenario())


def test_a_misbehaving_server_becomes_a_typed_tool_error(tmp_path: Path) -> None:
    async def scenario() -> None:
        client = _FakeMcpClient(fail=True)
        tool = (await mcp_tools(client, CancellationSource().token))[0]

        with pytest.raises(ToolExecutionError) as failure:
            await tool.execute(_context(tmp_path), {"query": "x"}, CancellationSource().token)

        assert "fixture" in failure.value.message

    asyncio.run(scenario())


def test_a_large_mcp_result_is_externalized_like_any_other(tmp_path: Path) -> None:
    async def scenario() -> None:
        payload = {"body": "M" * 60_000}
        client = _FakeMcpClient(payload)
        tools = await mcp_tools(
            client,
            CancellationSource().token,
            policy=McpToolPolicy(max_result_size_chars=4_000),
        )
        registry = ToolRegistry(tools)
        store = InMemoryToolResultStore()
        bus = InMemoryEventBus()
        events: list[RuntimeEvent] = []
        bus.subscribe(events.append)
        executor = ToolExecutor(
            registry,
            PolicyPermissionEngine(),
            store,
            bus,
            prompt=ScriptedPermissionPrompt((PermissionDecision.ALLOW,)),
        )

        result = await executor.execute(
            ModelToolCall("m1", "mcp__fixture__search_issues", {"query": "x"}),
            session_id="s",
            workspace=Workspace.from_path(tmp_path),
            cancellation=CancellationSource().token,
        )

        assert result.reference is not None
        assert isinstance(result.output, dict)
        assert result.output["externalized"] is True
        assert "M" * 5_000 not in str(result.output)
        stored = await store.get(result.reference, CancellationSource().token)
        assert payload["body"] in stored
        assert any(
            event.payload.get("externalized") is True
            for event in events
            if event.name is EventName.TOOL_COMPLETED
        )

    asyncio.run(scenario())


def test_cancelling_an_mcp_call_stops_it(tmp_path: Path) -> None:
    async def scenario() -> None:
        from athena.errors import CancellationError

        client = _FakeMcpClient()
        client.hang = True
        tool = (await mcp_tools(client, CancellationSource().token))[0]
        source = CancellationSource()
        task = asyncio.create_task(tool.execute(_context(tmp_path), {"query": "x"}, source.token))
        await asyncio.wait_for(client.started.wait(), timeout=5)

        source.cancel()

        with pytest.raises(CancellationError):
            await asyncio.wait_for(task, timeout=5)

    asyncio.run(scenario())


def test_an_mcp_call_honours_its_timeout(tmp_path: Path) -> None:
    async def scenario() -> None:
        from athena.errors import ProcessTimeoutError

        client = _FakeMcpClient()
        client.hang = True
        tool = McpTool(
            client,
            (await client.list_tools(CancellationSource().token))[0],
            McpToolPolicy(timeout_seconds=0.2),
        )

        with pytest.raises(ProcessTimeoutError):
            await tool.execute(_context(tmp_path), {"query": "x"}, CancellationSource().token)

    asyncio.run(scenario())
