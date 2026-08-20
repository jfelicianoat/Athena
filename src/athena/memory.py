"""The three levels of agent memory, and the minimal compaction between them.

Athena separates what it *said* from what it *knows*:

- **ConversationContext** is the transcript. It is disposable: a bounded, compactable
  window of recent messages, useful for the model's short-term coherence and nothing else.
- **WorkingMemory** is the structured operational state of one session — objective,
  constraints, plan, files, decisions, errors, verification. It is durable, validated, and
  the thing recovery restores. This is why compaction is safe: everything that matters was
  never in the transcript to begin with.
- **ProjectMemory** is knowledge that outlives a session. Only the interface exists here;
  nothing writes to it automatically, because a runtime that silently accumulates
  cross-session beliefs is far harder to reason about than one that does not.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Protocol, runtime_checkable

from athena.models import ModelMessage, ModelRole
from athena.working_state import WorkingState

#: WorkingMemory and WorkingState are the same object seen from two vocabularies:
#: `WorkingState` names what it is, `WorkingMemory` names the role it plays here.
WorkingMemory = WorkingState

_EXTERNALIZED_MARKER = "athena-result://"


@dataclass(frozen=True, slots=True)
class ConversationContext:
    """The disposable transcript window."""

    messages: tuple[ModelMessage, ...] = ()

    def appended(self, *messages: ModelMessage) -> ConversationContext:
        return replace(self, messages=(*self.messages, *messages))

    def with_messages(self, messages: tuple[ModelMessage, ...]) -> ConversationContext:
        return replace(self, messages=messages)

    @property
    def size_chars(self) -> int:
        return sum(len(message.content) for message in self.messages)

    def __len__(self) -> int:
        return len(self.messages)


@runtime_checkable
class ProjectMemory(Protocol):
    """Cross-session knowledge. Declared for later milestones; nothing implements it yet."""

    async def recall(self, query: str, *, limit: int = 10) -> tuple[str, ...]: ...

    async def remember(self, fact: str, *, source: str) -> None: ...


@dataclass(frozen=True, slots=True)
class CompactionReport:
    messages_before: int
    messages_after: int
    chars_before: int
    chars_after: int
    dropped_externalized: int = 0
    dropped_duplicates: int = 0
    truncated: int = 0
    reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def changed(self) -> bool:
        return self.messages_after != self.messages_before or self.chars_after != self.chars_before


class MicroCompaction:
    """The smallest compaction that is still honest.

    It never summarises with a model. It drops what is provably redundant and keeps the
    rest verbatim:

    - tool results already externalized keep their reference and lose their inline body;
    - repeated identical tool output keeps only the most recent occurrence;
    - over-long messages are truncated with an explicit marker;
    - the most recent turns are always kept intact.

    Nothing durable is lost, because objective, constraints, decisions, files, changes,
    errors, verification and remaining work live in WorkingMemory, which the context
    builder re-renders on every request.
    """

    def __init__(
        self,
        *,
        keep_recent: int = 6,
        max_message_chars: int = 4_000,
        stub_chars: int = 200,
    ) -> None:
        if keep_recent < 1:
            raise ValueError("keep_recent must be at least 1")
        self.keep_recent = keep_recent
        self.max_message_chars = max_message_chars
        self.stub_chars = stub_chars

    def compact(
        self, context: ConversationContext, working: WorkingMemory | None = None
    ) -> tuple[ConversationContext, CompactionReport]:
        del working  # Durable facts are re-rendered from WorkingMemory, not carried here.
        messages = list(context.messages)
        chars_before = context.size_chars
        if len(messages) <= self.keep_recent:
            return context, CompactionReport(
                len(messages), len(messages), chars_before, chars_before
            )

        head = messages[: -self.keep_recent]
        tail = messages[-self.keep_recent :]
        externalized = 0
        duplicates = 0
        truncated = 0
        seen_tool_output: dict[tuple[str | None, str], int] = {}
        kept: list[ModelMessage | None] = []

        for index, message in enumerate(head):
            content = message.content
            if message.role is ModelRole.TOOL and _EXTERNALIZED_MARKER in content:
                kept.append(replace(message, content=self._stub(content)))
                externalized += 1
                continue
            if message.role is ModelRole.TOOL:
                key = (message.name, content)
                previous = seen_tool_output.get(key)
                if previous is not None:
                    kept[previous] = None
                    duplicates += 1
                seen_tool_output[key] = index
            if len(content) > self.max_message_chars:
                kept.append(replace(message, content=self._truncate(content)))
                truncated += 1
                continue
            kept.append(message)

        compacted = tuple(item for item in kept if item is not None) + tuple(tail)
        result = context.with_messages(compacted)
        reasons: list[str] = []
        if externalized:
            reasons.append(f"{externalized} externalized tool result(s) reduced to a reference")
        if duplicates:
            reasons.append(f"{duplicates} repeated tool output(s) dropped")
        if truncated:
            reasons.append(f"{truncated} oversized message(s) truncated")
        return result, CompactionReport(
            messages_before=len(context.messages),
            messages_after=len(compacted),
            chars_before=chars_before,
            chars_after=result.size_chars,
            dropped_externalized=externalized,
            dropped_duplicates=duplicates,
            truncated=truncated,
            reasons=tuple(reasons),
        )

    def _stub(self, content: str) -> str:
        marker = content.find(_EXTERNALIZED_MARKER)
        reference = content[marker:].split('"')[0].split()[0] if marker >= 0 else ""
        return (
            f"{content[: self.stub_chars]}"
            f"\n[compacted: full output remains available at {reference}]"
        )

    def _truncate(self, content: str) -> str:
        return content[: self.max_message_chars] + "\n[compacted: message truncated]"


class ContextWindowManager:
    """Selects what goes to the model. It never concatenates the whole session."""

    def __init__(
        self,
        *,
        max_context_chars: int = 60_000,
        compaction: MicroCompaction | None = None,
    ) -> None:
        if max_context_chars <= 0:
            raise ValueError("max_context_chars must be positive")
        self.max_context_chars = max_context_chars
        self.compaction = compaction or MicroCompaction()

    def needs_compaction(self, context: ConversationContext) -> bool:
        return context.size_chars > self.max_context_chars

    def select(
        self, context: ConversationContext, working: WorkingMemory | None = None
    ) -> tuple[ConversationContext, CompactionReport | None]:
        """Return the window to send, compacting first when it would not fit."""
        if not self.needs_compaction(context):
            return context, None
        compacted, report = self.compaction.compact(context, working)
        if not self.needs_compaction(compacted):
            return compacted, report
        # Still too large: fall back to the most recent turns, which the working memory
        # makes safe to do.
        kept = compacted.messages[-self.compaction.keep_recent :]
        trimmed = compacted.with_messages(kept)
        return trimmed, replace(
            report,
            messages_after=len(kept),
            chars_after=trimmed.size_chars,
            reasons=(*report.reasons, "window trimmed to the most recent turns"),
        )


__all__ = [
    "CompactionReport",
    "ContextWindowManager",
    "ConversationContext",
    "MicroCompaction",
    "ProjectMemory",
    "WorkingMemory",
]
