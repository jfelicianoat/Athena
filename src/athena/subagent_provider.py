"""Wanting a delegate, and how one is produced, are separate questions.

`SubagentRunner` answers both at once: it decides that a delegate should run and it also
builds an `AgentLoop` to be that delegate. That is fine while Athena is the only thing that
can be a delegate, and it stops being fine the moment something else could — another
Athena over the network, a different harness, a mocked one in a test.

The seam here is deliberately thin. A provider is asked to start a delegate and hands back
the same `SubagentResult` everything downstream already understands, so nothing above has
to learn a second vocabulary. What a provider adds is an honest declaration of what it can
do, and a registry that refuses to hand work to one that cannot do it — because the failure
mode worth ruling out is not "no provider", it is "a provider that quietly did less".

Nothing here selects models, routes requests or re-implements the loop. The native provider
is a wrapper of about twenty lines; that is the intended size.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Protocol, runtime_checkable

from athena.cancellation import CancellationToken
from athena.errors import AthenaRuntimeError, ToolValidationError
from athena.subagents import (
    SubagentBrief,
    SubagentBudget,
    SubagentResult,
    SubagentRole,
    SubagentRunner,
)
from athena.types import JSONObject
from athena.workspace import Workspace


class UnsupportedCapabilityError(AthenaRuntimeError):
    """A provider was asked for a guarantee it does not offer.

    Raised rather than worked around. The alternative — running anyway and hoping the
    caller did not need what it asked for — is the silent degradation this whole seam
    exists to prevent, and it fails later, further away, and unattributably.
    """

    code = "unsupported_capability"


class NoSuitableProviderError(AthenaRuntimeError):
    """Nothing on the registry can satisfy the requirements."""

    code = "no_suitable_provider"


@dataclass(frozen=True, slots=True)
class SubagentCapabilities:
    """What a provider can actually guarantee about the delegates it starts.

    Declared, not inferred. A provider that says nothing is assumed to offer nothing,
    which is the reading that fails closed.
    """

    #: The delegate can be told to produce a machine-readable answer and will.
    structured_output: bool = False
    #: The delegate's toolset can be restricted to a subset before it starts.
    tool_filtering: bool = False
    #: The delegate works somewhere its siblings cannot see.
    isolated_workspace: bool = False
    #: The delegate survives its task and can be given more work later.
    continuation: bool = False
    #: The delegate stops when told to, promptly, all the way down.
    cancellation: bool = False
    #: Progress is observable before the delegate finishes.
    streaming: bool = False
    #: How deep a chain of delegates this provider will allow. `None` means it does not
    #: bound it, which is different from allowing any depth: nothing here promises that.
    depth_limit: int | None = None

    def satisfies(self, required: SubagentCapabilities) -> tuple[str, ...]:
        """Which requirements this provider fails to meet. Empty means it qualifies.

        Returns the gaps rather than a boolean because "no" without "which" leaves the
        caller unable to say anything useful about why nothing was suitable.
        """
        gaps: list[str] = []
        for flag in (
            "structured_output",
            "tool_filtering",
            "isolated_workspace",
            "continuation",
            "cancellation",
            "streaming",
        ):
            if getattr(required, flag) and not getattr(self, flag):
                gaps.append(flag)
        # A depth limit is a number, not a flag: asking for five levels of a provider that
        # allows three is a mismatch even though both "support" depth.
        if (
            required.depth_limit is not None
            and self.depth_limit is not None
            and self.depth_limit < required.depth_limit
        ):
            gaps.append("depth_limit")
        return tuple(gaps)


@dataclass(frozen=True, slots=True)
class SubagentStartRequest:
    """One delegation, in terms that do not name an implementation."""

    role: SubagentRole
    brief: SubagentBrief
    workspace: Workspace
    cancellation: CancellationToken
    parent_session_id: str = ""
    budget: SubagentBudget | None = None
    #: What this particular delegation needs to be true, over and above the role.
    requires: SubagentCapabilities = field(default_factory=SubagentCapabilities)

    def to_json(self) -> JSONObject:
        return {
            "role": self.role.value,
            "objective": self.brief.objective,
            "parent_session_id": self.parent_session_id,
        }


@runtime_checkable
class Continuable(Protocol):
    """Lo que hace falta para volver a preguntarle al mismo delegado.

    Aparte de `Delegator` a proposito: continuar exige recordar al hijo entre llamadas, y
    no todo delegador puede —uno remoto o sin estado, por ejemplo—. Meterlo en el Protocol
    principal obligaria a todos a declarar que saben hacerlo, y el que no supiera fallaria
    al ejecutarlo en vez de al declararse.
    """

    async def follow_up(
        self,
        session_id: str,
        question: str,
        workspace: Workspace,
        parent_cancellation: CancellationToken,
        *,
        parent_session_id: str = ...,
    ) -> SubagentResult: ...

    def follow_ups_left(self, session_id: str) -> int:
        """Cuantas veces mas se le puede preguntar a ese delegado. Cero si ninguna.

        Un numero y no la sesion entera: quien pregunta solo necesita decidir si le sale a
        cuenta seguir con este o pedir otro, y devolverle el objeto le invitaria a tocar
        cosas que no le tocan.
        """
        ...


@runtime_checkable
class Delegator(Protocol):
    """Lo que el ejecutor necesita saber de quien ejecuta tareas: que delega.

    `SubagentRunner` y `SubagentService` cumplen esta forma, así que el grafo puede recibir
    cualquiera de los dos sin enterarse. Es el punto entero de la costura: quien coordina
    no elige implementación, sólo pide trabajo.
    """

    async def delegate(
        self,
        role: SubagentRole,
        brief: SubagentBrief,
        workspace: Workspace,
        parent_cancellation: CancellationToken,
        *,
        parent_session_id: str = ...,
        budget: SubagentBudget | None = ...,
    ) -> SubagentResult: ...


@runtime_checkable
class SubagentProvider(Protocol):
    """Something that can be a delegate."""

    @property
    def name(self) -> str: ...

    def capabilities(self) -> SubagentCapabilities: ...

    async def start(self, request: SubagentStartRequest) -> SubagentResult: ...


class NativeAthenaSubagentProvider:
    """Athena itself, which is what a delegate has been until now.

    A wrapper and nothing more. The isolation, the profiles and the budgets stay in
    `SubagentRunner`, which already implements them and has the tests to say so.
    """

    def __init__(self, runner: SubagentRunner, *, name: str = "native") -> None:
        self._runner = runner
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def capabilities(self) -> SubagentCapabilities:
        """What Athena delegates genuinely offer today.

        `continuation` era falso porque no existia, y declararlo habria sido un deseo en
        vez de un hecho. Ahora existe —ver ADR-030— y sigue siendo un hecho: quien lo lea
        puede volver a preguntarle a un delegado, dentro de su tope.

        `streaming` sigue siendo falso por el mismo motivo por el que lo era.
        `isolated_workspace` tambien: los delegados comparten workspace y lo que los separa
        es el cerrojo de escritura, que no es aislamiento.
        """
        return SubagentCapabilities(
            structured_output=True,
            tool_filtering=True,
            isolated_workspace=False,
            continuation=True,
            cancellation=True,
            streaming=False,
            depth_limit=1,
        )

    async def start(self, request: SubagentStartRequest) -> SubagentResult:
        return await self._runner.delegate(
            request.role,
            request.brief,
            request.workspace,
            request.cancellation,
            parent_session_id=request.parent_session_id,
            budget=request.budget,
        )

    async def follow_up(
        self,
        session_id: str,
        question: str,
        workspace: Workspace,
        parent_cancellation: CancellationToken,
        *,
        parent_session_id: str = "",
    ) -> SubagentResult:
        return await self._runner.follow_up(
            session_id,
            question,
            workspace,
            parent_cancellation,
            parent_session_id=parent_session_id,
        )

    def follow_ups_left(self, session_id: str) -> int:
        return self._runner.follow_ups_left(session_id)


class SubagentProviderRegistry:
    """Which providers exist, and which of them can do a given piece of work."""

    def __init__(self, providers: tuple[SubagentProvider, ...] = ()) -> None:
        self._providers: dict[str, SubagentProvider] = {}
        for provider in providers:
            self.register(provider)

    def register(self, provider: SubagentProvider) -> None:
        if provider.name in self._providers:
            raise AthenaRuntimeError(f"A provider is already registered as {provider.name!r}")
        self._providers[provider.name] = provider

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._providers)

    def all(self) -> tuple[SubagentProvider, ...]:
        """Todos, en orden de registro, que es el orden de preferencia."""
        return tuple(self._providers.values())

    def select(self, required: SubagentCapabilities) -> SubagentProvider:
        """The first provider that meets every requirement, or an error naming the gaps.

        Registration order is the preference order. Choosing by "most capable" would pick
        a heavier provider for work that did not need it, and choosing by "closest match"
        would need a notion of distance nobody has defined.
        """
        if not self._providers:
            raise NoSuitableProviderError("No subagent provider is registered")
        rejected: dict[str, tuple[str, ...]] = {}
        for name, provider in self._providers.items():
            gaps = provider.capabilities().satisfies(required)
            if not gaps:
                return provider
            rejected[name] = gaps
        raise NoSuitableProviderError(
            "No registered subagent provider offers what this task requires",
            details={"rejected": {name: list(gaps) for name, gaps in rejected.items()}},
        )


class SubagentService:
    """What the executor talks to.

    Deliberately exposes `delegate` with the signature `SubagentRunner` already had, so the
    executor keeps depending on a shape rather than on a class and nothing above had to be
    rewritten to gain the seam.
    """

    def __init__(self, registry: SubagentProviderRegistry) -> None:
        self.registry = registry

    async def delegate(
        self,
        role: SubagentRole,
        brief: SubagentBrief,
        workspace: Workspace,
        parent_cancellation: CancellationToken,
        *,
        parent_session_id: str = "",
        budget: SubagentBudget | None = None,
        requires: SubagentCapabilities | None = None,
    ) -> SubagentResult:
        request = SubagentStartRequest(
            role=role,
            brief=brief,
            workspace=workspace,
            cancellation=parent_cancellation,
            parent_session_id=parent_session_id,
            budget=budget,
            requires=requires or _required_for(role),
        )
        provider = self.registry.select(request.requires)
        return await provider.start(request)

    async def follow_up(
        self,
        session_id: str,
        question: str,
        workspace: Workspace,
        parent_cancellation: CancellationToken,
        *,
        parent_session_id: str = "",
    ) -> SubagentResult:
        """Volver a preguntarle a un delegado, sea de quien sea.

        Se enruta al proveedor que declara saber continuar **y** que conoce a ese
        delegado. Preguntarle al primero que dijera que si mandaria el seguimiento a quien
        no tiene ni idea de quien es, y contestaria empezando de cero con la etiqueta de
        otro.
        """
        proveedor = self._owner(session_id)
        if proveedor is None:
            raise ToolValidationError(f"Ningun proveedor reconoce al delegado {session_id}")
        return await proveedor.follow_up(
            session_id,
            question,
            workspace,
            parent_cancellation,
            parent_session_id=parent_session_id,
        )

    def follow_ups_left(self, session_id: str) -> int:
        proveedor = self._owner(session_id)
        return 0 if proveedor is None else proveedor.follow_ups_left(session_id)

    def _owner(self, session_id: str) -> Continuable | None:
        for provider in self.registry.all():
            if not provider.capabilities().continuation:
                continue
            if isinstance(provider, Continuable) and provider.follow_ups_left(session_id) > 0:
                return provider
        return None


def _required_for(role: SubagentRole) -> SubagentCapabilities:
    """What a role needs from whoever runs it, regardless of who that is.

    Every delegate must be stoppable and must be confinable to a toolset — those are not
    conveniences, they are the two things that make delegation safe. An explorer
    additionally has to answer in a shape the parent can read without a model, because its
    whole purpose is to hand findings upwards.
    """
    base = SubagentCapabilities(tool_filtering=True, cancellation=True)
    if role is SubagentRole.EXPLORER:
        return replace(base, structured_output=True)
    return base


__all__ = [
    "Delegator",
    "NativeAthenaSubagentProvider",
    "NoSuitableProviderError",
    "SubagentCapabilities",
    "SubagentProvider",
    "SubagentProviderRegistry",
    "SubagentService",
    "SubagentStartRequest",
    "UnsupportedCapabilityError",
]
