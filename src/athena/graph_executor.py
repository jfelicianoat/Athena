"""Runs a validated `TaskGraph` by driving the machinery that already exists.

This is the piece that turns three libraries into a runtime. `planning.py` decided the
shape of the work, `tasks.py` knows how to bound and cancel a unit of it, `subagents.py`
knows how to run one in isolation, and until now nothing joined them up. The executor is
that join and deliberately little else — it starts nothing the `TaskManager` would not
start, isolates nothing the `SubagentRunner` would not isolate, and proves nothing the
`VerificationPolicy` would not prove.

It does not contain a second agent loop. A task is executed by handing a brief to
`SubagentRunner`, which builds an `AgentLoop` exactly as it already did; there is one loop
implementation in Athena and this uses it.

Three rules shape everything below.

**Reads run together, writes run alone.** An explorer and a verifier looking at the same
repository cannot interfere; two coders editing it can, and will, in ways that are painful
to reproduce. Until worktrees exist, writers take a lock — the boring answer, and the one
that cannot corrupt a workspace.

**A task passing is not the goal passing.** Each task proves its own small thing; the goal
is proved once at the end, against the project's real checks, over the whole result. A
runtime that reported success because every part reported success would be trusting a sum
of local claims about a global property.

**The parent receives evidence, not a transcript.** What comes back up is a summary, the
files touched, the commands run and the verification — never the child's conversation. That
is what keeps the parent's context from growing with every delegate it uses.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

from athena.cancellation import CancellationScope, CancellationToken, chained_source
from athena.errors import AthenaRuntimeError
from athena.events import EventBus, EventName, RuntimeEvent
from athena.planning import PlanBoard, PlanStatus, TaskGraph, TaskNode
from athena.state import ExecutionOutcome, SessionState, classify_outcome
from athena.subagents import SubagentBrief, SubagentResult, SubagentRole, SubagentRunner
from athena.tasks import TaskBudget, TaskManager
from athena.types import JSONObject
from athena.verification import VerificationPolicy, VerificationResult
from athena.workspace import Workspace

#: Roles that may change the workspace. Everything else runs concurrently, because two
#: readers cannot produce a conflict and serialising them would waste the parallelism the
#: graph was built to expose.
_WRITING_ROLES = frozenset({SubagentRole.CODER})


@dataclass(frozen=True, slots=True)
class TaskEvidence:
    """What a finished task hands upwards.

    Everything here is a fact the child produced, not a claim it made about itself.
    `summary` is the one piece of prose, and it exists because a person reading a run needs
    a sentence, not a file list.
    """

    task_id: str
    role: SubagentRole
    outcome: ExecutionOutcome
    summary: str = ""
    files_changed: tuple[str, ...] = ()
    commands_run: tuple[str, ...] = ()
    facts: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    unresolved: tuple[str, ...] = ()
    verification: VerificationResult | None = None
    error_code: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.outcome is ExecutionOutcome.COMPLETED

    def to_json(self) -> JSONObject:
        return {
            "task_id": self.task_id,
            "role": self.role.value,
            "outcome": self.outcome.value,
            "summary": self.summary,
            "files_changed": list(self.files_changed),
            "commands_run": list(self.commands_run),
            "facts": list(self.facts),
            "risks": list(self.risks),
            "unresolved": list(self.unresolved),
            "verification": (None if self.verification is None else self.verification.status.value),
            "error_code": self.error_code,
        }


@dataclass(frozen=True, slots=True)
class GraphResult:
    """How the whole plan ended, and what it can show for it."""

    outcome: ExecutionOutcome
    graph: TaskGraph
    evidence: tuple[TaskEvidence, ...] = ()
    goal_verification: VerificationResult | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def files_changed(self) -> tuple[str, ...]:
        seen: dict[str, None] = {}
        for item in self.evidence:
            for path in item.files_changed:
                seen[path] = None
        return tuple(seen)

    def evidence_for(self, task_id: str) -> TaskEvidence | None:
        return next((item for item in self.evidence if item.task_id == task_id), None)

    def to_json(self) -> JSONObject:
        return {
            "outcome": self.outcome.value,
            "tasks": [item.to_json() for item in self.evidence],
            "files_changed": list(self.files_changed),
            "goal_verification": (
                None
                if self.goal_verification is None
                else {
                    "status": self.goal_verification.status.value,
                    "summary": self.goal_verification.summary,
                }
            ),
            "duration_seconds": round((self.finished_at - self.started_at).total_seconds(), 3),
        }


class GraphExecutor:
    """Drives a `TaskGraph` to a verified conclusion, or stops trying and says why."""

    def __init__(
        self,
        runner: SubagentRunner,
        manager: TaskManager,
        event_bus: EventBus,
        *,
        goal_verification: VerificationPolicy | None = None,
        task_verification: VerificationPolicy | None = None,
        board: PlanBoard | None = None,
        max_parallel_reads: int = 4,
    ) -> None:
        self.runner = runner
        self.manager = manager
        self.event_bus = event_bus
        #: Run once, at the end, over the whole result. This is what decides the goal.
        self.goal_verification = goal_verification
        #: Optional, and run only after a task that actually changed something. Finding out
        #: at the end that the third task broke the build is worse than finding out then.
        self.task_verification = task_verification
        #: Donde otros —una interfaz, un canal— pueden leer el plan en curso. El
        #: executor no sabe quién mira ni le importa; deja el grafo puesto.
        self.board = board
        self.max_parallel_reads = max(1, max_parallel_reads)
        #: One writer at a time in a shared workspace. Worktrees would remove the need for
        #: this; until they exist, a lock is the only honest answer.
        self._write_lock = asyncio.Lock()

    async def execute(
        self,
        graph: TaskGraph,
        workspace: Workspace,
        cancellation: CancellationToken,
        *,
        run_id: str = "",
    ) -> GraphResult:
        """Work the ready frontier until the graph is done, blocked, or stopped."""
        started = datetime.now(UTC)
        evidence: list[TaskEvidence] = []
        outcome = ExecutionOutcome.COMPLETED

        if self.board is not None:
            self.board.record(run_id or "graph", graph)
        await self._publish(EventName.GRAPH_STARTED, run_id, {"tasks": len(graph)})
        try:
            while not graph.is_complete():
                cancellation.raise_if_cancelled()
                frontier = graph.ready()
                if not frontier:
                    # Nothing can start and nothing is running: whatever remains is blocked
                    # behind something that failed. Reporting COMPLETED here would be the
                    # runtime declaring victory over work it never did.
                    outcome = ExecutionOutcome.FAILED
                    break
                batch = await self._run_frontier(frontier, graph, workspace, cancellation, run_id)
                evidence.extend(batch)
        except AthenaRuntimeError as error:
            outcome = classify_outcome(error)
            if not outcome.is_stopped_deliberately:
                raise

        goal = None
        if outcome is ExecutionOutcome.COMPLETED:
            goal = await self._verify_goal(graph, workspace, cancellation, run_id)
            if goal is not None and not goal.permits_completion:
                # Every part reported success and the whole does not work. That is exactly
                # the case goal verification exists to catch.
                outcome = ExecutionOutcome.FAILED

        result = GraphResult(
            outcome=outcome,
            graph=graph,
            evidence=tuple(evidence),
            goal_verification=goal,
            started_at=started,
            finished_at=datetime.now(UTC),
        )
        await self._publish(
            EventName.GRAPH_COMPLETED
            if outcome is ExecutionOutcome.COMPLETED
            else EventName.GRAPH_CANCELLED
            if outcome.is_stopped_deliberately
            else EventName.GRAPH_FAILED,
            run_id,
            result.to_json(),
        )
        return result

    # -- the frontier ------------------------------------------------------

    async def _run_frontier(
        self,
        frontier: Sequence[TaskNode],
        graph: TaskGraph,
        workspace: Workspace,
        cancellation: CancellationToken,
        run_id: str,
    ) -> list[TaskEvidence]:
        """Run everything that can start now, respecting the read/write rule.

        The whole frontier is launched together and the lock decides what actually
        overlaps. Partitioning it here instead would mean deciding the order in advance,
        which is the scheduler's job and it does not need help.
        """
        for node in frontier:
            if node.status is PlanStatus.PENDING:
                graph.transition(node.id, PlanStatus.READY)

        reads = asyncio.Semaphore(self.max_parallel_reads)
        results = await asyncio.gather(
            *(
                self._run_task(node, graph, workspace, cancellation, run_id, reads)
                for node in frontier
            ),
            return_exceptions=True,
        )

        collected: list[TaskEvidence] = []
        for node, outcome in zip(frontier, results, strict=True):
            if isinstance(outcome, BaseException):
                if not isinstance(outcome, AthenaRuntimeError):
                    raise outcome
                collected.append(self._failure_evidence(node, outcome, graph))
                continue
            collected.append(outcome)
        return collected

    async def _run_task(
        self,
        node: TaskNode,
        graph: TaskGraph,
        workspace: Workspace,
        cancellation: CancellationToken,
        run_id: str,
        reads: asyncio.Semaphore,
    ) -> TaskEvidence:
        writes = node.suggested_role in _WRITING_ROLES
        # Writers queue behind the lock; readers behind a semaphore that bounds how many
        # model calls are in flight at once rather than what they may touch.
        gate: asyncio.Lock | asyncio.Semaphore = self._write_lock if writes else reads
        async with gate:
            cancellation.raise_if_cancelled()
            graph.transition(node.id, PlanStatus.RUNNING)
            await self._publish(
                EventName.TASK_STARTED,
                run_id,
                {"task_id": node.id, "role": node.suggested_role.value, "goal": node.goal},
                correlation_id=node.id,
            )
            evidence = await self._delegate(node, graph, workspace, cancellation, run_id)

        graph.transition(
            node.id,
            PlanStatus.COMPLETED if evidence.succeeded else PlanStatus.FAILED,
            verification=evidence.to_json(),
        )
        await self._publish(
            EventName.TASK_COMPLETED if evidence.succeeded else EventName.TASK_FAILED,
            run_id,
            evidence.to_json(),
            correlation_id=node.id,
        )
        return evidence

    async def _delegate(
        self,
        node: TaskNode,
        graph: TaskGraph,
        workspace: Workspace,
        cancellation: CancellationToken,
        run_id: str,
    ) -> TaskEvidence:
        """Hand one task to a subagent, through the task manager that bounds it."""
        del run_id
        brief = self._brief_for(node, graph)
        scoped = chained_source(cancellation, CancellationScope.TASK)

        async def body(token: CancellationToken, tracker: object) -> SubagentResult:
            del tracker
            return await self.runner.delegate(
                node.suggested_role,
                brief,
                workspace,
                token,
                parent_session_id=node.id,
            )

        task_id = self.manager.submit(
            f"task:{node.id}",
            body,
            budget=_budget_for(node),
            parent_cancellation=scoped.token,
        )
        try:
            outcome = await self.manager.wait(task_id)
        except AthenaRuntimeError as error:
            return self._failure_evidence(node, error, graph, transition=False)

        if not isinstance(outcome, SubagentResult):  # pragma: no cover - body returns one
            raise AthenaRuntimeError("A delegated task returned something unexpected")
        evidence = _evidence_from(node, outcome)
        if evidence.succeeded and self.task_verification is not None and evidence.files_changed:
            evidence = replace(
                evidence,
                verification=await self._verify_task(node, workspace, cancellation),
            )
            if evidence.verification is not None and not evidence.verification.permits_completion:
                evidence = replace(evidence, outcome=ExecutionOutcome.FAILED)
        return evidence

    def _brief_for(self, node: TaskNode, graph: TaskGraph) -> SubagentBrief:
        """What the delegate is told, which is its task and its dependencies' findings.

        Not the parent's conversation, and not the whole graph. A task that needed to know
        everything would not have been worth separating.
        """
        findings: list[str] = []
        for dependency in node.dependencies:
            upstream = graph.get(dependency)
            recorded = upstream.verification or {}
            summary = recorded.get("summary")
            if isinstance(summary, str) and summary.strip():
                findings.append(f"{dependency}: {summary}")
        return SubagentBrief(
            objective=node.goal,
            acceptance_criteria=node.acceptance_criteria,
            relevant_files=node.inputs,
            findings=tuple(findings),
            constraints=(f"Expected output: {node.expected_output}",),
        )

    # -- verification ------------------------------------------------------

    async def _verify_task(
        self, node: TaskNode, workspace: Workspace, cancellation: CancellationToken
    ) -> VerificationResult | None:
        if self.task_verification is None:
            return None
        return await self.task_verification.verify(
            _session_for(node.id, workspace.workspace_id), workspace, cancellation
        )

    async def _verify_goal(
        self,
        graph: TaskGraph,
        workspace: Workspace,
        cancellation: CancellationToken,
        run_id: str,
    ) -> VerificationResult | None:
        if self.goal_verification is None:
            return None
        await self._publish(EventName.VERIFICATION_STARTED, run_id, {"scope": "goal"})
        result = await self.goal_verification.verify(
            _session_for(run_id or "goal", workspace.workspace_id), workspace, cancellation
        )
        await self._publish(
            EventName.VERIFICATION_COMPLETED
            if result.permits_completion
            else EventName.VERIFICATION_FAILED,
            run_id,
            {"scope": "goal", "status": result.status.value, "summary": result.summary},
        )
        del graph
        return result

    # -- odds and ends -----------------------------------------------------

    def _failure_evidence(
        self,
        node: TaskNode,
        error: AthenaRuntimeError,
        graph: TaskGraph,
        *,
        transition: bool = True,
    ) -> TaskEvidence:
        outcome = classify_outcome(error)
        evidence = TaskEvidence(
            task_id=node.id,
            role=node.suggested_role,
            outcome=outcome,
            summary=error.message,
            error_code=error.code,
        )
        if transition and graph.get(node.id).status is PlanStatus.RUNNING:
            graph.transition(node.id, PlanStatus.FAILED, verification=evidence.to_json())
        return evidence

    async def _publish(
        self,
        name: EventName,
        run_id: str,
        payload: JSONObject,
        *,
        correlation_id: str | None = None,
    ) -> None:
        await self.event_bus.publish(RuntimeEvent(name, run_id or "graph", payload, correlation_id))


def _budget_for(node: TaskNode) -> TaskBudget:
    """A task's share of the run's allowance.

    Deliberately per-task rather than global: a graph whose budget is one shared pool lets
    the first task spend everything, and the failure looks like the last task's fault.
    """
    del node
    return TaskBudget(max_iterations=8, max_tool_calls=40, wall_clock_seconds=600.0)


def _session_for(identifier: str, workspace_id: str = "") -> SessionState:
    """The minimum a `VerificationPolicy` needs to be asked a question."""
    return SessionState(session_id=identifier, workspace_id=workspace_id)


def _evidence_from(node: TaskNode, result: SubagentResult) -> TaskEvidence:
    """Compose what goes upward. Everything else the child produced stays with the child."""
    outcome = ExecutionOutcome.COMPLETED if result.succeeded else ExecutionOutcome.FAILED
    if result.error is not None:
        outcome = classify_outcome(result.error)
    facts: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    unresolved: tuple[str, ...] = ()
    if result.role is SubagentRole.EXPLORER and result.answer:
        report = result.explorer_report()
        facts = report.findings
        risks = report.risks
        unresolved = report.recommended_next_steps
    elif result.role is SubagentRole.VERIFIER and result.answer:
        verifier = result.verifier_report()
        risks = verifier.failures
        facts = verifier.evidence
    return TaskEvidence(
        task_id=node.id,
        role=result.role,
        outcome=outcome,
        summary=(result.answer or "").strip()[:2_000],
        files_changed=result.files_modified,
        commands_run=result.commands_run,
        facts=facts,
        risks=risks,
        unresolved=unresolved,
        error_code=None if result.error is None else result.error.code,
    )


__all__ = ["GraphExecutor", "GraphResult", "TaskEvidence"]
