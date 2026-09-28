"""Regresiones de la auditoria del 28-sep-2026 (`Informes/Athena-2026-09-28`).

Cada prueba nombra el hallazgo que defiende (A01, A02...). Se escriben contra los
caminos reales —`RunRegistry`, `ToolExecutor`, `CommandPolicy`— y no contra piezas
sueltas: la auditoria mostro que una pieza aprobada aisladamente no demuestra que una
funcion este bien conectada.
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from athena.adapters.service import CapabilityMode, RunOptions, RunRegistry
from athena.adapters.service.orchestration import ExecutionMode, OrchestrationSettings
from athena.agent_loop import AgentRunResult, AgentRunStatus
from athena.cancellation import CancellationSource, CancellationToken
from athena.checkpoints import CheckpointStore
from athena.errors import AthenaRuntimeError, PermissionDeniedError, ToolValidationError
from athena.events import EventName, InMemoryEventBus, RuntimeEvent
from athena.hooks import HookRegistry
from athena.model_catalog import ModelCatalog
from athena.models import ModelRequest, ModelResponse, ModelToolCall
from athena.mutation_tools import workspace_mutation_tools
from athena.permissions import (
    PermissionDecision,
    PermissionPolicy,
    PermissionRequest,
    PolicyPermissionEngine,
    RiskLevel,
    RiskTier,
)
from athena.planning import TaskGraph
from athena.process_tools import BashTool, CommandPolicy, parse_command, run_process
from athena.process_tree import ProcessTreeError, reap
from athena.project_memory import MemoryKind, SqliteProjectMemory, VerificationState
from athena.registry import ToolRegistry
from athena.rollback import RollbackLedger, RollbackResult, RollbackScope, checkpointing_hooks
from athena.session_store import SessionRecord, SqliteSessionStore
from athena.state import AgentStatus, SessionState
from athena.stores import InMemoryToolResultStore
from athena.subagents import SubagentRole
from athena.testing import FakeModelProvider
from athena.tool_executor import ToolExecutor
from athena.types import JSONValue
from athena.verification import (
    ArtifactVerificationPolicy,
    VerificationResult,
    VerificationStatus,
)
from athena.working_state import WorkingState
from athena.workspace import Workspace

_MARKER_TEST = (
    "import unittest\n"
    "from pathlib import Path\n\n\n"
    "class Marker(unittest.TestCase):\n"
    "    def test_marker(self):\n"
    "        p = Path('verification_marker.txt')\n"
    "        p.write_text((p.read_text() if p.exists() else '') + 'executed\\n')\n"
)


def _project_with_checks(root: Path) -> Path:
    """Un proyecto cuyos checks dejan huella si alguien los ejecuta."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "AGENTS.md").write_text(
        "## Verification\n\n```text\npython -m unittest -q\n```\n", encoding="utf-8"
    )
    (root / "test_marker.py").write_text(_MARKER_TEST, encoding="utf-8")
    return root


def _registry(tmp_path: Path, answers: int = 4) -> tuple[RunRegistry, list[RuntimeEvent]]:
    bus = InMemoryEventBus()
    seen: list[RuntimeEvent] = []
    bus.subscribe(seen.append)
    provider = FakeModelProvider(
        [ModelResponse("Descripcion del proyecto.", "fake", "stop")] * answers
    )
    registry = RunRegistry(
        provider,
        bus,
        SqliteSessionStore(tmp_path / "state" / "sessions.db"),
        InMemoryToolResultStore(),
    )
    return registry, seen


async def _run(
    registry: RunRegistry,
    root: Path,
    options: RunOptions,
    objective: str = "Describe el proyecto",
) -> AgentRunResult:
    try:
        run_id = await registry.start(objective, Workspace.from_path(root), options)
        return await registry.wait(run_id)
    finally:
        await registry.shutdown()


# --------------------------------------------------------------------------- A01


@pytest.mark.parametrize("execution", [CapabilityMode.OFF, CapabilityMode.ASK])
def test_a01_checks_do_not_run_without_execution_authority(
    tmp_path: Path, execution: CapabilityMode
) -> None:
    """Con ejecucion `off`, o con `ask` sin nadie que apruebe, no se ejecuta nada del
    proyecto: ni para la linea base ni para verificar."""
    root = _project_with_checks(tmp_path / "repo")
    registry, seen = _registry(tmp_path)
    result = asyncio.run(
        _run(registry, root, RunOptions(writes=CapabilityMode.OFF, execution=execution))
    )

    assert not (root / "verification_marker.txt").exists()
    assert not any(e.name is EventName.VERIFICATION_CHECK_STARTED for e in seen)
    # No se da por bueno lo que no se comprobo, y se dice por que.
    assert result.status is not AgentRunStatus.COMPLETED
    assert result.error is not None
    assert result.error.details.get("reason") == "execution_not_authorized"


def test_a01_ask_mode_shows_the_exact_commands_before_running(tmp_path: Path) -> None:
    root = _project_with_checks(tmp_path / "repo")
    registry, seen = _registry(tmp_path)
    asyncio.run(
        _run(registry, root, RunOptions(writes=CapabilityMode.OFF, execution=CapabilityMode.ASK))
    )

    requested = [
        e
        for e in seen
        if e.name is EventName.PERMISSION_REQUESTED and e.payload.get("tool_name") == "verification"
    ]
    assert requested, "la verificacion tiene que pedir permiso como cualquier tool"
    effects = requested[0].payload["possible_effects"]
    assert isinstance(effects, list)
    assert "$ python -m unittest -q" in effects


def test_a01_allow_runs_the_checks(tmp_path: Path) -> None:
    root = _project_with_checks(tmp_path / "repo")
    registry, _ = _registry(tmp_path)
    asyncio.run(
        _run(registry, root, RunOptions(writes=CapabilityMode.OFF, execution=CapabilityMode.ALLOW))
    )

    # Linea base y verificacion: dos ejecuciones, las dos autorizadas.
    assert (root / "verification_marker.txt").read_text() == "executed\nexecuted\n"


# --------------------------------------------------------------------------- A02


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        # Lo que la auditoria encontro clasificado como lectura o build.
        ("git branch -D example", RiskTier.R3_EXTERNAL_OR_IRREVERSIBLE),
        (
            "git remote add example https://example.invalid/repo",
            RiskTier.R3_EXTERNAL_OR_IRREVERSIBLE,
        ),
        ("git diff --output=audit-output.txt", RiskTier.R3_EXTERNAL_OR_IRREVERSIBLE),
        ("ruff check --fix .", RiskTier.R3_EXTERNAL_OR_IRREVERSIBLE),
        ("ruff format .", RiskTier.R3_EXTERNAL_OR_IRREVERSIBLE),
        ("npm run deploy", RiskTier.R3_EXTERNAL_OR_IRREVERSIBLE),
        ("uv run python -c print(123)", RiskTier.R4_FORBIDDEN),
        ("git tag example", RiskTier.R4_FORBIDDEN),
        # Opciones que ejecutan o cambian lo que se ejecuta.
        ("git -c core.pager=evil log", RiskTier.R4_FORBIDDEN),
        ("git diff --ext-diff", RiskTier.R4_FORBIDDEN),
        ("find . -delete", RiskTier.R4_FORBIDDEN),
        ("find . -exec rm {} +", RiskTier.R4_FORBIDDEN),
        ("rg --pre evil pattern", RiskTier.R4_FORBIDDEN),
        ("npx something", RiskTier.R4_FORBIDDEN),
        # Lo que sigue siendo lo que parece.
        ("git branch", RiskTier.R2_LOCAL_EXECUTION),
        ("git branch -a", RiskTier.R2_LOCAL_EXECUTION),
        ("git remote -v", RiskTier.R2_LOCAL_EXECUTION),
        ("ruff check .", RiskTier.R2_LOCAL_EXECUTION),
        ("ruff format --check .", RiskTier.R2_LOCAL_EXECUTION),
        ("npm run test", RiskTier.R2_LOCAL_EXECUTION),
        ("uv run pytest -q", RiskTier.R2_LOCAL_EXECUTION),
        ("uv run --with pytest pytest", RiskTier.R2_LOCAL_EXECUTION),
        ("python -m pytest -q", RiskTier.R2_LOCAL_EXECUTION),
    ],
)
def test_a02_command_classification_matrix(
    tmp_path: Path, command: str, expected: RiskTier
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    classification = CommandPolicy().classify(parse_command(command), ".", workspace_root=root)
    assert classification.tier is expected, classification.reason


@pytest.mark.parametrize(
    "command",
    [
        "python -m json.tool ../outside.json",
        "cat ../outside.json",
        "git -C .. status",
        "git diff --output=../audit-output.txt",
        "grep secret ..",
        "uv run --directory .. pytest",
    ],
)
def test_a02_arguments_that_name_paths_outside_the_workspace_are_refused(
    tmp_path: Path, command: str
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    classification = CommandPolicy().classify(parse_command(command), ".", workspace_root=root)
    assert classification.tier is RiskTier.R4_FORBIDDEN
    assert "outside the workspace" in classification.reason


def test_a02_the_full_tool_path_does_not_read_outside_the_workspace(tmp_path: Path) -> None:
    """La reproduccion de la auditoria, por el `ToolExecutor` real y con ejecucion permitida."""
    root = tmp_path / "repo"
    root.mkdir()
    (tmp_path / "outside.json").write_text('{"secret": "fuera"}', encoding="utf-8")
    executor = ToolExecutor(
        ToolRegistry((BashTool(),)),
        PolicyPermissionEngine(PermissionPolicy(allow_local_execution=True)),
        InMemoryToolResultStore(),
        InMemoryEventBus(),
    )
    call = ModelToolCall("outside", "bash", {"command": "python -m json.tool ../outside.json"})
    with pytest.raises(PermissionDeniedError):
        asyncio.run(
            executor.execute(
                call,
                session_id="a02",
                workspace=Workspace.from_path(root),
                cancellation=CancellationSource().token,
            )
        )


@pytest.mark.parametrize(
    ("executable", "sub", "attempt"),
    [
        ("git", "push", "read"),
        ("git", "reset", "build"),
        ("npm", "publish", "build"),
        ("git", "commit", "read"),
    ],
)
def test_a02_an_extension_never_loosens_a_built_in_rule(
    executable: str, sub: str, attempt: str
) -> None:
    before = CommandPolicy().classify((executable, sub), ".").tier
    after = (
        CommandPolicy(subcommands={executable: {sub: attempt}})
        .classify((executable, sub), ".")
        .tier
    )
    assert after is before


def test_a02_an_extension_can_still_add_and_tighten() -> None:
    policy = CommandPolicy(subcommands={"just": {"test": "build"}, "git": {"status": "forbidden"}})
    assert policy.classify(("just", "test"), ".").tier is RiskTier.R2_LOCAL_EXECUTION
    assert policy.classify(("git", "status"), ".").tier is RiskTier.R4_FORBIDDEN


# --------------------------------------------------------------------------- A05


def _graph_run(
    tmp_path: Path,
    responses: list[ModelResponse],
    options: RunOptions,
    objective: str = "Escribe report.txt",
    models: ModelCatalog | None = None,
) -> tuple[RunRegistry, _DelayedProvider, Path]:
    root = tmp_path / "repo"
    root.mkdir(exist_ok=True)
    provider = _DelayedProvider(responses)
    registry = RunRegistry(
        provider,
        InMemoryEventBus(),
        SqliteSessionStore(tmp_path / "state" / "hierarchy.db"),
        InMemoryToolResultStore(),
        orchestration=OrchestrationSettings(planning=True),
        models=models,
    )
    return registry, provider, root


class _DelayedProvider(FakeModelProvider):
    async def complete(
        self, request: ModelRequest, cancellation: CancellationToken
    ) -> ModelResponse:
        await asyncio.sleep(0.05)
        return await super().complete(request, cancellation)


def _writes_report() -> list[ModelResponse]:
    return [
        ModelResponse("plan no valido", "fake", "stop"),
        ModelResponse(
            "",
            "fake",
            "tool_calls",
            tool_calls=(
                ModelToolCall(
                    "write", "write_file", {"path": "report.txt", "content": "Informe real"}
                ),
            ),
        ),
        ModelResponse("report.txt producido", "fake", "stop"),
    ]


def test_a05_a_hierarchical_documents_run_keeps_the_provenance_of_what_it_wrote(
    tmp_path: Path,
) -> None:
    registry, _, root = _graph_run(
        tmp_path,
        _writes_report(),
        RunOptions(),
    )
    options = RunOptions(
        writes=CapabilityMode.ALLOW,
        execution=CapabilityMode.OFF,
        profile="documents",
        deliverables=("report.txt",),
        execution_mode=ExecutionMode.HIERARCHICAL,
    )
    result = asyncio.run(_run(registry, root, options, "Escribe report.txt"))
    assert (root / "report.txt").read_text(encoding="utf-8") == "Informe real"
    assert result.status is AgentRunStatus.COMPLETED, (
        None if result.verification is None else result.verification.summary
    )


def test_a05_a_preexisting_file_nobody_wrote_is_not_a_deliverable(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "report.txt").write_text("Estaba antes", encoding="utf-8")
    workspace = Workspace.from_path(root)
    result = asyncio.run(
        ArtifactVerificationPolicy(["report.txt"]).verify(
            SessionState("s", workspace.workspace_id), workspace, CancellationSource().token
        )
    )
    assert result.status is VerificationStatus.FAILED
    assert "exists but was not written by this run" in result.summary


def test_a05_missing_and_empty_are_told_apart(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "empty.txt").write_text("", encoding="utf-8")
    workspace = Workspace.from_path(root)
    state = SessionState("s", workspace.workspace_id, attributes={"files_modified": ["empty.txt"]})
    result = asyncio.run(
        ArtifactVerificationPolicy(["missing.txt", "empty.txt"]).verify(
            state, workspace, CancellationSource().token
        )
    )
    assert "missing.txt does not exist" in result.summary
    assert "empty.txt is empty" in result.summary


# --------------------------------------------------------------------------- A06


def test_a06_the_chosen_model_reaches_every_call_of_a_hierarchical_run(tmp_path: Path) -> None:
    registry, provider, root = _graph_run(
        tmp_path, _writes_report(), RunOptions(), models=ModelCatalog(["chosen-model"])
    )
    options = RunOptions(
        writes=CapabilityMode.ALLOW,
        execution=CapabilityMode.OFF,
        profile="documents",
        deliverables=("report.txt",),
        model="chosen-model",
        execution_mode=ExecutionMode.HIERARCHICAL,
    )
    asyncio.run(_run(registry, root, options, "Escribe report.txt"))
    assert provider.requests, "el run no llego a llamar al modelo"
    assert {request.model for request in provider.requests} == {"chosen-model"}


def test_a06_the_run_deadline_governs_the_graph(tmp_path: Path) -> None:
    registry, _, root = _graph_run(tmp_path, _writes_report() * 3, RunOptions())
    options = RunOptions(
        writes=CapabilityMode.ALLOW,
        execution=CapabilityMode.OFF,
        profile="documents",
        deliverables=("report.txt",),
        execution_mode=ExecutionMode.HIERARCHICAL,
        session_timeout_seconds=0.01,
    )
    result = asyncio.run(_run(registry, root, options, "Escribe report.txt"))
    assert result.status is not AgentRunStatus.COMPLETED
    assert not (root / "report.txt").exists(), "el plazo no alcanzo a los hijos"


def test_a06_a_hierarchical_run_refuses_a_goal_revision_it_would_not_apply(
    tmp_path: Path,
) -> None:
    registry, _, root = _graph_run(tmp_path, _writes_report(), RunOptions())
    options = RunOptions(
        writes=CapabilityMode.ALLOW,
        execution=CapabilityMode.OFF,
        profile="documents",
        deliverables=("report.txt",),
        execution_mode=ExecutionMode.HIERARCHICAL,
    )

    async def scenario() -> None:
        try:
            run_id = await registry.start("Escribe report.txt", Workspace.from_path(root), options)
            with pytest.raises(ToolValidationError, match="plan de tareas"):
                registry.revise_goal(run_id, "solo analiza; no crees archivos", base_revision=1)
            await registry.wait(run_id)
        finally:
            await registry.shutdown()

    asyncio.run(scenario())


# --------------------------------------------------------------------------- A07


def _interrupted_documents_run(tmp_path: Path) -> tuple[SqliteSessionStore, str, Path]:
    root = tmp_path / "repo"
    root.mkdir()
    sessions = SqliteSessionStore(tmp_path / "state" / "sessions.db")
    registry = RunRegistry(
        FakeModelProvider([ModelResponse("done", "fake", "stop")]),
        InMemoryEventBus(),
        sessions,
        InMemoryToolResultStore(),
    )
    options = RunOptions(
        writes=CapabilityMode.OFF,
        execution=CapabilityMode.OFF,
        profile="documents",
        deliverables=("report.txt",),
        max_iterations=3,
        session_timeout_seconds=45,
    )

    async def scenario() -> str:
        try:
            run_id = await registry.start("Escribe report.txt", Workspace.from_path(root), options)
            await registry.wait(run_id)
        finally:
            await registry.shutdown()
        record = await sessions.load(run_id)
        assert record is not None
        await sessions.save(replace(record, status=AgentStatus.RECOVERY_PENDING))
        return run_id

    return sessions, asyncio.run(scenario()), root


def test_a07_a_cold_resume_keeps_the_authorized_configuration(tmp_path: Path) -> None:
    sessions, run_id, root = _interrupted_documents_run(tmp_path)
    cold = RunRegistry(
        FakeModelProvider([ModelResponse("resumed", "fake", "stop")]),
        InMemoryEventBus(),
        sessions,
        InMemoryToolResultStore(),
    )

    async def scenario() -> None:
        try:
            resumed = await cold.resume(run_id, Workspace.from_path(root))
            live = cold.run(resumed)
            assert live.options.writes is CapabilityMode.OFF
            assert live.options.execution is CapabilityMode.OFF
            assert live.options.profile == "documents"
            assert live.options.deliverables == ("report.txt",)
            assert live.options.max_iterations <= 3
            assert live.options.session_timeout_seconds <= 45
            assert live.goal is not None
            await cold.wait(resumed)
        finally:
            await cold.shutdown()

    asyncio.run(scenario())


def test_a07_a_run_is_not_resumed_in_another_project(tmp_path: Path) -> None:
    sessions, run_id, _ = _interrupted_documents_run(tmp_path)
    other = tmp_path / "otro-proyecto"
    other.mkdir()
    cold = RunRegistry(
        FakeModelProvider([ModelResponse("resumed", "fake", "stop")]),
        InMemoryEventBus(),
        sessions,
        InMemoryToolResultStore(),
    )

    async def scenario() -> None:
        try:
            with pytest.raises(ToolValidationError, match="belongs to"):
                await cold.resume(run_id, Workspace.from_path(other))
        finally:
            await cold.shutdown()

    asyncio.run(scenario())


# --------------------------------------------------------------------------- A08


def test_a08_the_same_folder_is_the_same_project(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    assert Workspace.from_path(root).workspace_id == Workspace.from_path(root).workspace_id
    assert (
        Workspace.from_path(root).workspace_id == Workspace.from_path(str(root) + "/").workspace_id
    )
    other = tmp_path / "otro"
    other.mkdir()
    assert Workspace.from_path(root).workspace_id != Workspace.from_path(other).workspace_id


def test_a08_project_memory_survives_from_one_run_to_the_next(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    memory = SqliteProjectMemory(tmp_path / "memory.db")

    async def scenario() -> tuple[int, int]:
        first = Workspace.from_path(root)
        item = await memory.propose(
            first.workspace_id, MemoryKind.VERIFIED_COMMAND, "python -m unittest", source="a08"
        )
        await memory.approve(item.id, state=VerificationState.VERIFIED, confidence=0.9)
        second = Workspace.from_path(root)
        other = tmp_path / "otro"
        other.mkdir()
        return (
            len(await memory.search(second.workspace_id, "unittest")),
            len(await memory.search(Workspace.from_path(other).workspace_id, "unittest")),
        )

    same, isolated = asyncio.run(scenario())
    assert same == 1
    assert isolated == 0


# --------------------------------------------------------------------------- A09


def test_a09_a_reader_does_not_overlap_the_writer_of_the_same_graph(tmp_path: Path) -> None:
    from test_graph_executor import _AnsweringProvider, _executor, node

    root = tmp_path / "repo"
    root.mkdir()
    workspace = Workspace.from_path(root)
    provider = _AnsweringProvider(delay=0.08)

    async def scenario() -> None:
        executor, manager = _executor(workspace, provider, InMemoryEventBus())
        graph = TaskGraph.build(
            [node("inspect", role=SubagentRole.EXPLORER), node("edit", role=SubagentRole.CODER)]
        )
        try:
            await executor.execute(graph, workspace, CancellationSource().token)
        finally:
            await manager.shutdown()

    asyncio.run(scenario())
    assert provider.peak_in_flight == 1


def test_a09_two_graphs_on_one_folder_do_not_write_at_once(tmp_path: Path) -> None:
    from test_graph_executor import _AnsweringProvider, _executor, node

    root = tmp_path / "repo"
    root.mkdir()
    workspace = Workspace.from_path(root)
    provider = _AnsweringProvider(delay=0.08)

    async def scenario() -> None:
        first, first_manager = _executor(workspace, provider, InMemoryEventBus())
        second, second_manager = _executor(workspace, provider, InMemoryEventBus())
        try:
            await asyncio.gather(
                first.execute(
                    TaskGraph.build([node("one", role=SubagentRole.CODER)]),
                    workspace,
                    CancellationSource().token,
                ),
                second.execute(
                    TaskGraph.build([node("two", role=SubagentRole.CODER)]),
                    workspace,
                    CancellationSource().token,
                ),
            )
        finally:
            await first_manager.shutdown()
            await second_manager.shutdown()

    asyncio.run(scenario())
    assert provider.peak_in_flight == 1


def test_a09_readers_of_one_folder_still_run_together(tmp_path: Path) -> None:
    from test_graph_executor import _AnsweringProvider, _executor, node

    root = tmp_path / "repo"
    root.mkdir()
    workspace = Workspace.from_path(root)
    provider = _AnsweringProvider(delay=0.08)

    async def scenario() -> None:
        executor, manager = _executor(workspace, provider, InMemoryEventBus())
        graph = TaskGraph.build(
            [node("a", role=SubagentRole.EXPLORER), node("b", role=SubagentRole.EXPLORER)]
        )
        try:
            await executor.execute(graph, workspace, CancellationSource().token)
        finally:
            await manager.shutdown()

    asyncio.run(scenario())
    assert provider.peak_in_flight == 2


# --------------------------------------------------------------------------- A10-A13


def _write_executor(workspace: Workspace, ledger: RollbackLedger) -> ToolExecutor:
    """El camino real de una escritura: ToolExecutor, permisos y los ganchos de deshacer."""
    return ToolExecutor(
        ToolRegistry(workspace_mutation_tools()),
        PolicyPermissionEngine(PermissionPolicy(allow_workspace_writes=True)),
        InMemoryToolResultStore(),
        InMemoryEventBus(),
        hooks=HookRegistry(checkpointing_hooks(ledger, workspace)),
    )


def _write(
    executor: ToolExecutor, workspace: Workspace, call_id: str, **arguments: JSONValue
) -> None:
    asyncio.run(
        executor.execute(
            ModelToolCall(call_id, "write_file", dict(arguments)),
            session_id="run-a10",
            workspace=workspace,
            cancellation=CancellationSource().token,
        )
    )


def test_a10_a_file_the_run_created_is_removed_by_the_rollback(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    workspace = Workspace.from_path(root)
    ledger = RollbackLedger(CheckpointStore(tmp_path / "cp"), run_id="run-a10")
    _write(_write_executor(workspace, ledger), workspace, "c1", path="new.txt", content="nuevo")
    assert (root / "new.txt").exists()
    assert len(ledger.points()) == 1

    result = asyncio.run(ledger.roll_back(workspace, scope=RollbackScope.RUN))
    assert result.restored == ("new.txt",)
    assert not (root / "new.txt").exists()


def test_a10_a_write_that_failed_is_not_counted_as_written(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "keep.txt").write_text("de la persona", encoding="utf-8")
    workspace = Workspace.from_path(root)
    ledger = RollbackLedger(CheckpointStore(tmp_path / "cp"), run_id="run-a10")
    executor = _write_executor(workspace, ledger)
    # Sin overwrite=true la herramienta se niega: la escritura no ocurre.
    with pytest.raises(AthenaRuntimeError):
        _write(executor, workspace, "c1", path="keep.txt", content="pisado")

    result = asyncio.run(ledger.roll_back(workspace, scope=RollbackScope.RUN))
    assert result.restored == ()
    assert (root / "keep.txt").read_text(encoding="utf-8") == "de la persona"


def test_a11_a_later_human_edit_of_the_same_file_is_not_lost(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    shared = root / "shared.txt"
    shared.write_text("original", encoding="utf-8")
    workspace = Workspace.from_path(root)
    ledger = RollbackLedger(CheckpointStore(tmp_path / "cp"))

    async def scenario() -> RollbackResult:
        await ledger.checkpoint("audit", workspace, ["shared.txt"])
        shared.write_text("Athena edit", encoding="utf-8")
        ledger.record_written("audit", ["shared.txt"])
        shared.write_text("subsequent human edit", encoding="utf-8")
        return await ledger.roll_back(workspace, scope=RollbackScope.RUN)

    result = asyncio.run(scenario())
    assert shared.read_text(encoding="utf-8") == "subsequent human edit"
    assert result.conflicts == ("shared.txt",)
    assert result.restored == ()


def test_a11_two_edits_by_the_run_unwind_to_the_original(tmp_path: Path) -> None:
    """Defecto encontrado al corregir A11: deshacer un run solo aplicaba la copia mas nueva."""
    root = tmp_path / "repo"
    root.mkdir()
    target = root / "calc.py"
    target.write_text("original", encoding="utf-8")
    workspace = Workspace.from_path(root)
    ledger = RollbackLedger(CheckpointStore(tmp_path / "cp"))

    async def scenario() -> None:
        for step in ("primera", "segunda"):
            await ledger.checkpoint(step, workspace, ["calc.py"])
            target.write_text(step, encoding="utf-8")
            ledger.record_written(step, ["calc.py"])
        await ledger.roll_back(workspace, scope=RollbackScope.RUN)

    asyncio.run(scenario())
    assert target.read_text(encoding="utf-8") == "original"


def test_a12_a_user_file_named_like_the_manifest_round_trips(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "nested").mkdir(parents=True)
    for name in ("checkpoint.json", "manifest.json", "nested/manifest.json"):
        (root / name).write_text('{"user_data": true}', encoding="utf-8")
    (root / "empty.txt").write_text("", encoding="utf-8")
    workspace = Workspace.from_path(root)
    store = CheckpointStore(tmp_path / "cp")
    names = ["checkpoint.json", "manifest.json", "nested/manifest.json", "empty.txt"]
    point = store.create(workspace, names)
    for name in names:
        (root / name).write_text("cambiado", encoding="utf-8")
    store.restore(point, workspace)
    for name in names[:3]:
        assert json.loads((root / name).read_text(encoding="utf-8")) == {"user_data": True}
    assert (root / "empty.txt").read_text(encoding="utf-8") == ""


def test_a12_a_damaged_copy_is_refused_and_the_original_is_untouched(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "a.txt").write_text("copia buena", encoding="utf-8")
    workspace = Workspace.from_path(root)
    store = CheckpointStore(tmp_path / "cp")
    point = store.create(workspace, ["a.txt"])
    (tmp_path / "cp" / point.checkpoint_id / "files" / "a.txt").write_text("rota", encoding="utf-8")
    (root / "a.txt").write_text("trabajo actual", encoding="utf-8")
    with pytest.raises(AthenaRuntimeError, match="damaged"):
        store.restore(point, workspace)
    assert (root / "a.txt").read_text(encoding="utf-8") == "trabajo actual"


def test_a13_the_ability_to_undo_survives_a_restart(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    target = root / "calc.py"
    target.write_text("original", encoding="utf-8")
    workspace = Workspace.from_path(root)
    store = CheckpointStore(tmp_path / "cp")
    first = RollbackLedger(store, run_id="run-a13")

    async def before_restart() -> None:
        await first.checkpoint("run-a13", workspace, ["calc.py"], scope=RollbackScope.RUN)
        target.write_text("cambio de Athena", encoding="utf-8")
        first.record_written("run-a13", ["calc.py"])

    asyncio.run(before_restart())
    # Otro proceso: un libro nuevo, reconstruido solo con lo que hay en disco.
    reloaded = RollbackLedger.load(CheckpointStore(tmp_path / "cp"), "run-a13")
    assert len(reloaded.points()) == 1
    assert RollbackLedger.load(store, "otro-run").points() == ()

    result = asyncio.run(reloaded.roll_back(Workspace.from_path(root), scope=RollbackScope.RUN))
    assert result.restored == ("calc.py",)
    assert target.read_text(encoding="utf-8") == "original"
    # Una vez usado no se vuelve a ofrecer.
    assert RollbackLedger.load(store, "run-a13").points() == ()


# --------------------------------------------------------------------------- A14

_ORPHANING_PARENT = (
    "import subprocess, sys\n"
    "subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]])\n"
    "print('parent leaves')\n"
)
_HEARTBEAT = (
    "import sys, time\n"
    "from pathlib import Path\n"
    "marker = Path(sys.argv[1])\n"
    "for _ in range(600):\n"
    "    with marker.open('a') as handle:\n"
    "        handle.write('.')\n"
    "    time.sleep(0.05)\n"
)


@pytest.mark.skipif(sys.platform != "win32", reason="Job Objects son de Windows")
def test_a14_an_orphaned_grandchild_does_not_outlive_its_command(tmp_path: Path) -> None:
    """El padre termina y deja un nieto vivo: `taskkill /T` ya no lo alcanzaria."""
    parent = tmp_path / "parent.py"
    child = tmp_path / "child.py"
    marker = tmp_path / "heartbeat.txt"
    parent.write_text(_ORPHANING_PARENT, encoding="utf-8")
    child.write_text(_HEARTBEAT, encoding="utf-8")

    async def scenario() -> None:
        exit_code, _, _ = await run_process(
            (sys.executable, str(parent), str(child), str(marker)),
            cwd=tmp_path,
            timeout_seconds=30,
            cancellation=CancellationSource().token,
        )
        assert exit_code == 0
        for _ in range(100):
            if marker.exists() and marker.stat().st_size > 2:
                break
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.5)
        size = marker.stat().st_size if marker.exists() else 0
        await asyncio.sleep(0.6)
        after = marker.stat().st_size if marker.exists() else 0
        assert after == size, "el nieto siguio escribiendo despues de terminar el comando"

    asyncio.run(scenario())


def test_a14_a_tree_that_does_not_die_is_reported_not_waited_for(tmp_path: Path) -> None:
    async def scenario() -> None:
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "import time; time.sleep(30)"
        )
        try:
            # Nadie lo mato: `reap` no puede esperar indefinidamente.
            with pytest.raises(ProcessTreeError):
                await reap(process, timeout=0.3)
        finally:
            process.kill()
            await process.wait()

    asyncio.run(scenario())


# --------------------------------------------------------------------------- A20


def _parse(raw: bytes) -> tuple[int, str] | str:
    """Lo que contesta el parser a unos bytes: (estado, codigo) si los rechaza."""
    from athena.adapters.service.server.transporte import TransporteMixin, _BadRequest

    async def scenario() -> tuple[int, str] | str:
        reader = asyncio.StreamReader()
        reader.feed_data(raw)
        reader.feed_eof()
        parser = TransporteMixin.__new__(TransporteMixin)
        try:
            request = await parser._read_request(reader)
            if request is not None:
                await parser._read_body(reader, request)
        except _BadRequest as refused:
            return refused.status, refused.code
        return "accepted"

    return asyncio.run(scenario())


@pytest.mark.parametrize(
    ("raw", "status"),
    [
        (b"POST /v1/runs HTTP/1.1\r\nContent-Length: -1\r\n\r\n", 400),
        (b"POST /v1/runs HTTP/1.1\r\nContent-Length: abc\r\n\r\n", 400),
        (b"POST /v1/runs HTTP/1.1\r\nContent-Length: 4194305\r\n\r\n", 413),
        (b"POST /v1/runs HTTP/1.1\r\nContent-Length: 2\r\nContent-Length: 3\r\n\r\n", 400),
        (b"POST /v1/runs HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n", 400),
        (b"POST /v1/runs HTTP/1.1\r\nContent-Length: 10\r\n\r\nabc", 400),
        (b"NONSENSE\r\n\r\n", 400),
        (b"GET /v1/health HTTP/1.1\r\nNoColon\r\n\r\n", 400),
    ],
)
def test_a20_malformed_requests_get_a_proper_answer(raw: bytes, status: int) -> None:
    outcome = _parse(raw)
    assert isinstance(outcome, tuple), outcome
    assert outcome[0] == status


def test_a20_a_well_formed_request_is_still_accepted() -> None:
    raw = b"POST /v1/runs HTTP/1.1\r\nContent-Length: 2\r\n\r\n{}"
    assert _parse(raw) == "accepted"


def test_a20_a_slow_client_is_cut_off_and_the_body_waits_for_the_token(tmp_path: Path) -> None:
    """Contra el servicio real: cabecera a medias → 408; sin token → 401 sin leer cuerpo."""
    from athena.adapters.service import AthenaService, ServiceConfig
    from athena.adapters.service.server import transporte

    registry = RunRegistry(
        FakeModelProvider([]),
        InMemoryEventBus(),
        SqliteSessionStore(tmp_path / "sessions.db"),
        InMemoryToolResultStore(),
    )
    service = AthenaService(registry, ServiceConfig(port=0, token="secreto"))

    async def exchange(host: str, port: int, payload: bytes) -> bytes:
        reader, writer = await asyncio.open_connection(host, port)
        writer.write(payload)
        await writer.drain()
        answer = await asyncio.wait_for(reader.read(), timeout=5)
        writer.close()
        return answer

    async def scenario() -> None:
        original = transporte._HEADER_TIMEOUT
        transporte._HEADER_TIMEOUT = 0.2
        host, port = await service.start()
        try:
            slow = await exchange(host, port, b"GET /v1/health HTTP/1.1\r\n")
            assert slow.startswith(b"HTTP/1.1 408")
            # Anuncia 1 MiB y no lo manda: sin token no se espera al cuerpo.
            unauthorised = await exchange(
                host, port, b"POST /v1/runs HTTP/1.1\r\nContent-Length: 1048576\r\n\r\n"
            )
            assert unauthorised.startswith(b"HTTP/1.1 401")
        finally:
            transporte._HEADER_TIMEOUT = original
            await service.stop()

    asyncio.run(scenario())


# --------------------------------------------------------------------------- A23


def test_a23_a_restart_does_not_replay_an_update_already_received(tmp_path: Path) -> None:
    from athena_telegram.adapter import TelegramAdapter
    from athena_telegram.config import TelegramSecurity
    from test_telegram_adapter import ALLOWED_ID, _FakeApi, _update

    state = tmp_path / "telegram-updates.json"
    batch = [_update(7, "arregla el test")]

    async def receive_once(api: _FakeApi) -> str | None:
        adapter = TelegramAdapter(
            api,
            TelegramSecurity.open_to((ALLOWED_ID,)),
            idle_backoff_seconds=0.0,
            state_path=state,
        )
        await adapter.start()
        task = asyncio.ensure_future(adapter.receive())
        await asyncio.sleep(0.05)
        await adapter.stop()
        message = await asyncio.wait_for(task, timeout=2)
        return None if message is None else message.text

    assert asyncio.run(receive_once(_FakeApi([batch]))) == "arregla el test"
    # El proceso se cae antes de confirmar el offset y Telegram repite el lote.
    assert asyncio.run(receive_once(_FakeApi([batch]))) is None


def test_a23_ignored_updates_still_move_the_offset(tmp_path: Path) -> None:
    from athena_telegram.adapter import TelegramAdapter
    from athena_telegram.config import TelegramSecurity
    from test_telegram_adapter import ALLOWED_ID, _FakeApi

    stickers: list[JSONValue] = [
        {"update_id": 40, "edited_message": {}},
        {"update_id": 41, "poll": {}},
    ]
    adapter = TelegramAdapter(
        _FakeApi([stickers]), TelegramSecurity.open_to((ALLOWED_ID,)), idle_backoff_seconds=0.0
    )
    asyncio.run(adapter._poll())
    assert adapter._offset == 42


# --------------------------------------------------------------------------- A22


def _running_record(session_id: str) -> SessionRecord:
    return SessionRecord(
        session_id=session_id,
        workspace_id="proj-x",
        status=AgentStatus.RUNNING,
        working_memory=WorkingState(objective="algo"),
    )


def test_a22_databases_are_closed_after_each_operation(tmp_path: Path) -> None:
    database = tmp_path / "sessions.db"
    store = SqliteSessionStore(database)
    asyncio.run(store.save(_running_record("s1")))
    asyncio.run(store.load("s1"))
    memory = SqliteProjectMemory(tmp_path / "memory.db")
    asyncio.run(memory.search("proj-x", "nada"))
    # En Windows esto falla con «el archivo esta siendo utilizado» si queda abierta.
    for path in tmp_path.iterdir():
        path.unlink()
    assert not any(tmp_path.iterdir())


def test_a22_a_database_from_a_newer_athena_is_refused(tmp_path: Path) -> None:
    import sqlite3

    from athena.sqlite_support import SchemaVersionError

    database = tmp_path / "sessions.db"
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA user_version = 99")
    connection.commit()
    connection.close()
    with pytest.raises(SchemaVersionError):
        SqliteSessionStore(database)


def test_a22_a_live_session_of_another_process_is_not_taken(tmp_path: Path) -> None:
    import sqlite3
    import subprocess

    database = tmp_path / "sessions.db"
    store = SqliteSessionStore(database)
    asyncio.run(store.save(_running_record("ajena")))
    asyncio.run(store.save(_running_record("huerfana")))
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        connection = sqlite3.connect(database)
        connection.execute(
            "UPDATE session_owners SET pid = ? WHERE session_id = ?", (other.pid, "ajena")
        )
        connection.execute(
            "UPDATE session_owners SET pid = ? WHERE session_id = ?", (999_999_999, "huerfana")
        )
        connection.commit()
        connection.close()
        marked = asyncio.run(store.mark_interrupted())
    finally:
        other.kill()
        other.wait()
    assert marked == ("huerfana",)


# --------------------------------------------------------------------------- A21


def test_a21_a_command_with_huge_output_is_captured_within_bounds(tmp_path: Path) -> None:
    async def scenario() -> str:
        _, stdout, _ = await run_process(
            (sys.executable, "-c", "import sys; sys.stdout.write('x' * (5 * 1024 * 1024))"),
            cwd=tmp_path,
            timeout_seconds=60,
            cancellation=CancellationSource().token,
        )
        return stdout

    stdout = asyncio.run(scenario())
    assert len(stdout) < 2 * 1024 * 1024 + 200
    assert "bytes omitted" in stdout


def test_a21_an_oversized_provider_response_is_refused() -> None:
    import io

    from athena.adapters.lectura import read_bounded
    from athena.errors import ModelPermanentError

    class _Response:
        def __init__(self, data: bytes) -> None:
            self._stream = io.BytesIO(data)

        def read(self, amount: int = -1) -> bytes:
            return self._stream.read(amount)

    with pytest.raises(ModelPermanentError):
        read_bounded(_Response(b"x" * 2048), limit=1024)  # type: ignore[arg-type]
    assert read_bounded(_Response(b"ok"), limit=1024) == b"ok"  # type: ignore[arg-type]


def test_a21_finished_runs_do_not_stay_in_memory_forever(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from athena.adapters.service.runs import ciclo

    monkeypatch.setattr(ciclo, "_MAX_FINISHED_RUNS", 2)
    root = tmp_path / "repo"
    root.mkdir()
    registry = RunRegistry(
        FakeModelProvider([ModelResponse("hecho", "fake", "stop")] * 10),
        InMemoryEventBus(),
        SqliteSessionStore(tmp_path / "sessions.db"),
        InMemoryToolResultStore(),
    )

    async def scenario() -> int:
        try:
            for _ in range(6):
                run_id = await registry.start(
                    "Describe", Workspace.from_path(root), RunOptions(execution=CapabilityMode.OFF)
                )
                await registry.wait(run_id)
                await asyncio.sleep(0)
            return len(registry.live_ids())
        finally:
            await registry.shutdown()

    assert asyncio.run(scenario()) <= 3


# --------------------------------------------------------------------------- A19


def test_a19_replacing_a_real_assertion_with_a_trivial_one_is_detected() -> None:
    from athena.verification import ChangeIntegrityPolicy

    weakening = "-    assert calculate(2, 3) == 5\n+    assert True\n"
    findings = ChangeIntegrityPolicy().inspect(weakening)
    assert [finding.kind for finding in findings] == ["assertions_weakened"]
    genuine = "-    assert calculate(2, 3) == 5\n+    assert calculate(2, 3) == 6 - 1\n"
    assert ChangeIntegrityPolicy().inspect(genuine) == ()


def test_a19_a_change_the_person_made_before_the_run_is_not_blamed_on_it(tmp_path: Path) -> None:
    import subprocess

    from athena.verification import (
        ChecksNeverAuthorized,
        CommandVerificationPolicy,
        VerificationPlanner,
    )

    root = tmp_path / "repo"
    root.mkdir()
    (root / "calc.py").write_text("x = 1\n", encoding="utf-8")
    for command in (
        ["init", "-b", "main"],
        ["config", "user.email", "a@example.invalid"],
        ["config", "user.name", "A"],
        ["add", "."],
        ["commit", "-m", "base"],
    ):
        subprocess.run(["git", "-C", str(root), *command], check=True, capture_output=True)
    # La persona ya tenia esto sin commitear cuando pidio el trabajo.
    (root / "calc.py").write_text("x = 1  # noqa\n", encoding="utf-8")
    workspace = Workspace.from_path(root)
    policy = CommandVerificationPolicy(
        VerificationPlanner(workspace), authorizer=ChecksNeverAuthorized()
    )

    async def scenario() -> VerificationResult:
        await policy.capture_baseline(workspace, CancellationSource().token)
        return await policy.verify(
            SessionState("s", workspace.workspace_id), workspace, CancellationSource().token
        )

    result = asyncio.run(scenario())
    assert not any(item.kind == "integrity" for item in result.evidence)


def test_a19_the_plan_uses_the_project_interpreter_and_checks_formatting(tmp_path: Path) -> None:
    from athena.verification import VerificationPlanner

    root = tmp_path / "repo"
    scripts = root / ".venv" / ("Scripts" if sys.platform == "win32" else "bin")
    scripts.mkdir(parents=True)
    interpreter = scripts / ("python.exe" if sys.platform == "win32" else "python")
    interpreter.write_text("", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n[tool.ruff]\n", encoding="utf-8"
    )
    plan = VerificationPlanner(Workspace.from_path(root)).plan()
    commands = {check.name: check.command for check in plan.checks}
    assert commands["pytest"][0] == str(interpreter.resolve())
    assert "ruff-format" in commands
    assert commands["ruff-format"][-2:] == ("--check", ".")


# --------------------------------------------------------------------------- A25


def test_a25_a_folder_inside_another_repository_is_not_taken_for_one(tmp_path: Path) -> None:
    import subprocess

    from athena.isolation import _is_git_repository

    parent = tmp_path / "grande"
    child = parent / "subcarpeta"
    child.mkdir(parents=True)
    subprocess.run(
        ["git", "-C", str(parent), "init", "-b", "main"], check=True, capture_output=True
    )

    async def scenario() -> tuple[bool, bool]:
        token = CancellationSource().token
        return await _is_git_repository(parent, token), await _is_git_repository(child, token)

    assert asyncio.run(scenario()) == (True, False)


def test_a01_a_client_that_attaches_right_after_start_is_asked(tmp_path: Path) -> None:
    """La pregunta de la linea base llega antes de que el cliente se suscriba: la
    ventana de enganche es lo que hace que `exec=ask` pueda verificar."""
    from athena.adapters.service import approvals
    from athena.adapters.service.approvals import RemotePermissionPrompt

    root = _project_with_checks(tmp_path / "repo")
    attached = {"value": False}
    asked: list[str] = []

    def answer(pending: approvals.PendingApproval) -> None:
        asked.append(pending.request.tool_name)
        pending.future.set_result(PermissionDecision.ALLOW)

    async def scenario() -> None:
        prompt = RemotePermissionPrompt(
            approvals.ApprovalRegistry(),
            "run-a01",
            answer,
            lambda: attached["value"],
            attach_grace_seconds=2.0,
        )

        async def attach_later() -> None:
            await asyncio.sleep(0.3)
            attached["value"] = True

        request = PermissionRequest(
            "verification",
            "run_project_checks",
            Workspace.from_path(root),
            RiskLevel.MEDIUM,
            RiskTier.R2_LOCAL_EXECUTION,
            is_read_only=False,
            is_destructive=False,
        )
        waiting = asyncio.ensure_future(attach_later())
        decision = await prompt.confirm(request)
        await waiting
        assert decision is PermissionDecision.ALLOW

    asyncio.run(scenario())
    assert asked == ["verification"]
