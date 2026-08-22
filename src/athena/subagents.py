"""Three specialised delegates. Deliberately not a swarm.

The ordering principle is task first, agent second: you delegate because a piece of work
has a shape, not because spawning an agent is available. So there are exactly three
profiles, each defined by what it is *not* allowed to do:

- **Explorer** reads and reports. It has no way to change anything.
- **Coder** changes things, inside the workspace, with the smallest toolset that permits it.
- **Verifier** runs the checks and says what it found. It cannot quietly repair what it
  broke the news about.

Each delegate is a fresh runtime: its own registry containing only its toolset, its own
permission policy, its own budget and timeout, its own conversation starting empty. It
receives a brief, not a transcript. It returns a structured result, not a monologue.

Subagents cannot delegate. No profile carries a delegation tool and the runner refuses to
nest, because recursive agents are how a bounded task becomes an unbounded bill.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from uuid import uuid4

from athena.agent_loop import AgentLoop, AgentLoopConfig, AgentRunStatus
from athena.cancellation import CancellationSource, CancellationToken
from athena.context import ContextBuilder
from athena.errors import AthenaRuntimeError, ToolValidationError
from athena.events import EventBus, EventName, SubagentEvent
from athena.models import ModelProvider
from athena.permissions import (
    DenyingPermissionPrompt,
    PermissionPolicy,
    PermissionPrompt,
    PolicyPermissionEngine,
)
from athena.registry import ToolRegistry
from athena.stores import ToolResultStore
from athena.tool_executor import ToolExecutor
from athena.tools import Tool
from athena.types import JSONObject, JSONValue
from athena.verification import LoopCompletionVerificationPolicy
from athena.workspace import Workspace

_JSON_BLOCK = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_BARE_OBJECT = re.compile(r"(\{.*\})", re.DOTALL)


class SubagentRole(StrEnum):
    EXPLORER = "explorer"
    CODER = "coder"
    VERIFIER = "verifier"


@dataclass(frozen=True, slots=True)
class SubagentBudget:
    max_iterations: int = 6
    max_tool_calls: int = 30
    timeout_seconds: float = 300.0

    def __post_init__(self) -> None:
        if self.max_iterations <= 0 or self.max_tool_calls <= 0:
            raise ValueError("Subagent budgets must be positive")
        if self.timeout_seconds <= 0:
            raise ValueError("Subagent timeout must be positive")


@dataclass(frozen=True, slots=True)
class SubagentProfile:
    """A delegate's whole authority, declared in one place."""

    role: SubagentRole
    purpose: str
    toolsets: tuple[str, ...]
    policy: PermissionPolicy
    budget: SubagentBudget = field(default_factory=SubagentBudget)
    #: What the delegate must produce, stated to it verbatim.
    output_contract: str = ""

    def registry_for(self, catalog: Mapping[str, Tool]) -> ToolRegistry:
        """Build a registry holding only this profile's toolset.

        Enforcement is structural first: a tool a profile may not use is not in its
        registry at all, so refusing it does not depend on a policy being configured
        correctly. The policy is the second line, not the first.
        """
        missing = [name for name in self.toolsets if name not in catalog]
        if missing:
            raise ToolValidationError(
                f"{self.role.value} profile requires tools that are not available: "
                f"{', '.join(sorted(missing))}"
            )
        return ToolRegistry(catalog[name] for name in self.toolsets)


#: Read, search, and read git history. Nothing that writes exists in this list.
EXPLORER_TOOLSET = (
    "glob",
    "grep",
    "read_file",
    "read_range",
    "list_directory",
    "git_status",
    "git_diff",
    "git_log",
    "git_show",
)

#: The smallest set that can actually change code and see what changed.
CODER_TOOLSET = (
    "read_file",
    "read_range",
    "glob",
    "grep",
    "edit_file",
    "write_file",
    "bash",
    "git_status",
    "git_diff",
)

#: Enough to run the checks and read the result. No edit tool, by construction.
VERIFIER_TOOLSET = (
    "read_file",
    "read_range",
    "grep",
    "bash",
    "git_status",
    "git_diff",
)


EXPLORER_PROFILE = SubagentProfile(
    role=SubagentRole.EXPLORER,
    purpose="Investigate the repository and report what matters, changing nothing.",
    toolsets=EXPLORER_TOOLSET,
    policy=PermissionPolicy(allow_workspace_writes=False, allow_local_execution=False),
    budget=SubagentBudget(max_iterations=8, max_tool_calls=40, timeout_seconds=300.0),
    output_contract=(
        "Finish with a single JSON object and nothing else, using exactly these keys: "
        '{"relevant_files": [], "findings": [], "risks": [], "recommended_next_steps": []}. '
        "Every value is a list of strings."
    ),
)

CODER_PROFILE = SubagentProfile(
    role=SubagentRole.CODER,
    purpose="Make the smallest change that satisfies the acceptance criteria.",
    toolsets=CODER_TOOLSET,
    policy=PermissionPolicy(allow_workspace_writes=True, allow_local_execution=True),
    budget=SubagentBudget(max_iterations=12, max_tool_calls=60, timeout_seconds=600.0),
    output_contract=("Finish by stating which files you changed and why, in one short paragraph."),
)

VERIFIER_PROFILE = SubagentProfile(
    role=SubagentRole.VERIFIER,
    purpose="Run the checks and report the result. Do not repair anything.",
    toolsets=VERIFIER_TOOLSET,
    # Execution is granted because running the suite is the job; writing never is.
    policy=PermissionPolicy(allow_workspace_writes=False, allow_local_execution=True),
    budget=SubagentBudget(max_iterations=8, max_tool_calls=30, timeout_seconds=900.0),
    output_contract=(
        "Finish with a single JSON object and nothing else: "
        '{"passed": true|false, "checks": [], "failures": [], "evidence": []}. '
        "Report what you observed. Do not attempt a fix."
    ),
)

DEFAULT_PROFILES: Mapping[SubagentRole, SubagentProfile] = {
    SubagentRole.EXPLORER: EXPLORER_PROFILE,
    SubagentRole.CODER: CODER_PROFILE,
    SubagentRole.VERIFIER: VERIFIER_PROFILE,
}


@dataclass(frozen=True, slots=True)
class SubagentBrief:
    """Everything a delegate is told. Notably not: the parent's conversation."""

    objective: str
    acceptance_criteria: tuple[str, ...] = ()
    relevant_files: tuple[str, ...] = ()
    findings: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.objective.strip():
            raise ToolValidationError("A subagent brief needs an objective")

    def render(self, profile: SubagentProfile) -> str:
        sections = [
            f"You are Athena's {profile.role.value}. {profile.purpose}",
            f"Objective: {self.objective}",
        ]
        if self.acceptance_criteria:
            sections.append(
                "Acceptance criteria:\n"
                + "\n".join(f"- {item}" for item in self.acceptance_criteria)
            )
        if self.relevant_files:
            sections.append(
                "Relevant files:\n" + "\n".join(f"- {item}" for item in self.relevant_files)
            )
        if self.findings:
            sections.append(
                "What an earlier step found:\n" + "\n".join(f"- {item}" for item in self.findings)
            )
        if self.constraints:
            sections.append("Constraints:\n" + "\n".join(f"- {item}" for item in self.constraints))
        if self.notes:
            sections.append("Notes:\n" + "\n".join(f"- {item}" for item in self.notes))
        if profile.output_contract:
            sections.append(f"Output contract: {profile.output_contract}")
        return "\n\n".join(sections)


@dataclass(frozen=True, slots=True)
class ExplorerReport:
    relevant_files: tuple[str, ...] = ()
    findings: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    recommended_next_steps: tuple[str, ...] = ()
    #: True when the delegate did not produce parseable structure and prose was kept.
    unstructured: bool = False

    @classmethod
    def parse(cls, answer: str | None) -> ExplorerReport:
        """Read the contracted JSON, and degrade to prose rather than losing the work."""
        payload = _extract_object(answer or "")
        if payload is None:
            text = (answer or "").strip()
            return cls(findings=(text,) if text else (), unstructured=True)
        return cls(
            relevant_files=_strings(payload.get("relevant_files")),
            findings=_strings(payload.get("findings")),
            risks=_strings(payload.get("risks")),
            recommended_next_steps=_strings(payload.get("recommended_next_steps")),
        )

    def to_json(self) -> JSONObject:
        return {
            "relevant_files": list(self.relevant_files),
            "findings": list(self.findings),
            "risks": list(self.risks),
            "recommended_next_steps": list(self.recommended_next_steps),
            "unstructured": self.unstructured,
        }


@dataclass(frozen=True, slots=True)
class VerifierReport:
    passed: bool = False
    checks: tuple[str, ...] = ()
    failures: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    unstructured: bool = False

    @classmethod
    def parse(cls, answer: str | None) -> VerifierReport:
        payload = _extract_object(answer or "")
        if payload is None:
            text = (answer or "").strip()
            return cls(evidence=(text,) if text else (), unstructured=True)
        return cls(
            passed=bool(payload.get("passed")),
            checks=_strings(payload.get("checks")),
            failures=_strings(payload.get("failures")),
            evidence=_strings(payload.get("evidence")),
        )


@dataclass(frozen=True, slots=True)
class SubagentResult:
    role: SubagentRole
    status: AgentRunStatus
    session_id: str
    answer: str | None = None
    error: AthenaRuntimeError | None = None
    files_modified: tuple[str, ...] = ()
    commands_run: tuple[str, ...] = ()
    tool_call_ids: tuple[str, ...] = ()

    @property
    def succeeded(self) -> bool:
        return self.status is AgentRunStatus.COMPLETED

    def explorer_report(self) -> ExplorerReport:
        return ExplorerReport.parse(self.answer)

    def verifier_report(self) -> VerifierReport:
        return VerifierReport.parse(self.answer)


class SubagentRunner:
    """Builds and runs one isolated delegate at a time.

    The runner owns the isolation: it constructs a registry from the profile's toolset, a
    permission engine from the profile's policy, a budget from the profile's limits, and a
    cancellation token chained to the parent's. It does not pass the parent's conversation,
    working memory, session store or project memory to the child.
    """

    def __init__(
        self,
        provider: ModelProvider,
        catalog: Mapping[str, Tool],
        event_bus: EventBus,
        result_store: ToolResultStore,
        *,
        profiles: Mapping[SubagentRole, SubagentProfile] | None = None,
        prompt: PermissionPrompt | None = None,
    ) -> None:
        self.provider = provider
        self.catalog = dict(catalog)
        self.event_bus = event_bus
        self.result_store = result_store
        self.profiles = dict(profiles or DEFAULT_PROFILES)
        # An unattended delegate that meets an ASK must stop, not guess.
        self.prompt = prompt or DenyingPermissionPrompt()

    def profile_for(self, role: SubagentRole) -> SubagentProfile:
        try:
            return self.profiles[role]
        except KeyError as exc:
            raise ToolValidationError(f"No profile for role: {role}") from exc

    async def delegate(
        self,
        role: SubagentRole,
        brief: SubagentBrief,
        workspace: Workspace,
        parent_cancellation: CancellationToken,
        *,
        parent_session_id: str = "",
        budget: SubagentBudget | None = None,
    ) -> SubagentResult:
        profile = self.profile_for(role)
        limits = budget or profile.budget
        registry = profile.registry_for(self.catalog)
        executor = ToolExecutor(
            registry,
            PolicyPermissionEngine(profile.policy),
            self.result_store,
            self.event_bus,
            prompt=self.prompt,
        )
        loop = AgentLoop(
            self.provider,
            registry,
            executor,
            ContextBuilder(workspace),
            self.event_bus,
            # A delegate proves it produced an answer; the parent decides what that answer
            # is worth. Running the project's checks inside every child would verify the
            # same repository three times for one task.
            verification=LoopCompletionVerificationPolicy(),
            config=AgentLoopConfig(
                max_iterations=limits.max_iterations,
                max_tool_calls=limits.max_tool_calls,
                session_timeout_seconds=limits.timeout_seconds,
                max_repair_cycles=0,
                capture_baseline=False,
            ),
        )

        child = CancellationSource()
        unsubscribe = parent_cancellation.register(child.cancel)
        # El hijo se nombra antes de arrancar, no al terminar. Sus eventos viajan por el
        # mismo bus con su propia sesion, asi que quien mira sólo puede atribuirselos si
        # ya sabe cómo se llama: anunciarlo al final deja huérfano todo lo que hizo
        # mientras lo hacía, que es precisamente cuando alguien está mirando.
        child_session_id = str(uuid4())
        await self.event_bus.publish(
            SubagentEvent(
                EventName.SUBAGENT_STARTED,
                parent_session_id,
                {
                    "role": role.value,
                    "objective": brief.objective,
                    "toolsets": list(profile.toolsets),
                    "max_iterations": limits.max_iterations,
                    "max_tool_calls": limits.max_tool_calls,
                    "timeout_seconds": limits.timeout_seconds,
                    "session_id": child_session_id,
                },
                child_session_id,
            )
        )
        try:
            run = await loop.run(
                brief.render(profile), workspace, child.token, session_id=child_session_id
            )
        finally:
            unsubscribe()

        working = run.working_state
        result = SubagentResult(
            role=role,
            status=run.status,
            session_id=run.session.session_id,
            answer=run.answer,
            error=run.error,
            files_modified=working.files_modified if working else (),
            commands_run=working.commands_run if working else (),
            tool_call_ids=run.tool_call_ids,
        )
        await self._announce(result, parent_session_id)
        return result

    async def _announce(self, result: SubagentResult, parent_session_id: str) -> None:
        names = {
            AgentRunStatus.COMPLETED: EventName.SUBAGENT_COMPLETED,
            AgentRunStatus.FAILED: EventName.SUBAGENT_FAILED,
            AgentRunStatus.CANCELLED: EventName.SUBAGENT_CANCELLED,
        }
        payload: dict[str, JSONValue] = {
            "role": result.role.value,
            "status": result.status.value,
            "files_modified": list(result.files_modified),
            "tool_calls": len(result.tool_call_ids),
        }
        if result.error is not None:
            payload["error_code"] = result.error.code
            payload["message"] = result.error.message
        await self.event_bus.publish(
            SubagentEvent(names[result.status], parent_session_id, payload, result.session_id)
        )


def _extract_object(text: str) -> Mapping[str, object] | None:
    for pattern in (_JSON_BLOCK, _BARE_OBJECT):
        match = pattern.search(text)
        if match is None:
            continue
        try:
            payload = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(payload, Mapping):
            return payload
    return None


def _strings(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if not isinstance(value, Sequence):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def with_budget(profile: SubagentProfile, budget: SubagentBudget) -> SubagentProfile:
    return replace(profile, budget=budget)


__all__ = [
    "CODER_PROFILE",
    "DEFAULT_PROFILES",
    "EXPLORER_PROFILE",
    "VERIFIER_PROFILE",
    "ExplorerReport",
    "SubagentBrief",
    "SubagentBudget",
    "SubagentProfile",
    "SubagentResult",
    "SubagentRole",
    "SubagentRunner",
    "VerifierReport",
    "with_budget",
]
