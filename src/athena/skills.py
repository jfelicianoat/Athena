"""Skills: procedural knowledge, not capability.

A **Tool** is something Athena can do. A **Skill** is something Athena knows about how to
do it — a written procedure, selected when it is relevant, injected as instructions.

The distinction is load-bearing for security. A skill can say "run the migration checks
before touching the schema"; it cannot make Athena able to run them. `required_toolsets`
is a *precondition*, not a request: a skill whose toolsets are absent is simply not
applicable. Selecting a skill never registers a tool, never widens a permission tier and
never touches the policy. If skills could grant capability, installing one would be an
unaudited privilege escalation.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass, field

from athena.errors import ToolValidationError
from athena.types import JSONObject

_MAX_INSTRUCTION_CHARS = 20_000
_WORD = re.compile(r"[a-z0-9_]+")


@dataclass(frozen=True, slots=True)
class SkillManifest:
    """What a skill declares about itself."""

    name: str
    description: str
    version: str
    applicable_tasks: tuple[str, ...] = ()
    required_toolsets: tuple[str, ...] = ()
    instructions: str = ""
    metadata: JSONObject = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ToolValidationError("A skill needs a name")
        if not self.description.strip():
            raise ToolValidationError(f"Skill {self.name} needs a description")
        if not self.version.strip():
            raise ToolValidationError(f"Skill {self.name} needs a version")
        if len(self.instructions) > _MAX_INSTRUCTION_CHARS:
            raise ToolValidationError(
                f"Skill {self.name} instructions exceed {_MAX_INSTRUCTION_CHARS} characters"
            )

    def matches(self, task: str) -> int:
        """How strongly this skill claims the task. Zero means not applicable."""
        if not self.applicable_tasks:
            return 0
        words = set(_WORD.findall(task.lower()))
        score = 0
        for claim in self.applicable_tasks:
            claim_words = set(_WORD.findall(claim.lower()))
            if not claim_words:
                continue
            if claim_words <= words:
                score += 2 * len(claim_words)
            elif claim_words & words:
                score += len(claim_words & words)
        return score

    def satisfied_by(self, available_tools: Collection[str]) -> bool:
        return all(toolset in available_tools for toolset in self.required_toolsets)


@dataclass(frozen=True, slots=True)
class SkillSelection:
    skill: SkillManifest
    score: int
    reason: str


class SkillRegistry:
    """Holds skills and decides which are relevant. It never changes what Athena may do."""

    def __init__(self, skills: Iterable[SkillManifest] = ()) -> None:
        self._skills: dict[str, SkillManifest] = {}
        for skill in skills:
            self.register(skill)

    def register(self, skill: SkillManifest) -> None:
        if skill.name in self._skills:
            raise ValueError(f"Skill already registered: {skill.name}")
        self._skills[skill.name] = skill

    def get(self, name: str) -> SkillManifest:
        try:
            return self._skills[name]
        except KeyError as exc:
            raise ToolValidationError(
                f"Unknown skill: {name}", details={"skill_name": name}
            ) from exc

    def names(self) -> tuple[str, ...]:
        return tuple(self._skills)

    def select(
        self,
        task: str,
        available_tools: Collection[str],
        *,
        limit: int = 3,
    ) -> tuple[SkillSelection, ...]:
        """Pick the skills that both claim the task and can actually be followed.

        A skill whose required toolsets are missing is dropped, never accommodated: the
        answer to "this skill needs a tool you do not have" is to not use the skill.
        """
        selections: list[SkillSelection] = []
        for skill in self._skills.values():
            score = skill.matches(task)
            if score <= 0:
                continue
            if not skill.satisfied_by(available_tools):
                continue
            selections.append(
                SkillSelection(
                    skill,
                    score,
                    f"matched {score} point(s) with every required toolset available",
                )
            )
        selections.sort(key=lambda item: (-item.score, item.skill.name))
        return tuple(selections[:limit])

    def unavailable(self, task: str, available_tools: Collection[str]) -> tuple[SkillManifest, ...]:
        """Skills that claimed the task but were dropped for missing toolsets."""
        return tuple(
            skill
            for skill in self._skills.values()
            if skill.matches(task) > 0 and not skill.satisfied_by(available_tools)
        )


def render_skills(selections: Sequence[SkillSelection]) -> str:
    """Render selected skills as instructions for the model."""
    if not selections:
        return ""
    blocks = [
        f"[skill: {selection.skill.name} v{selection.skill.version}] "
        f"{selection.skill.description}\n{selection.skill.instructions}".strip()
        for selection in selections
    ]
    return "\n\n".join(blocks)


__all__ = [
    "SkillManifest",
    "SkillRegistry",
    "SkillSelection",
    "render_skills",
]
