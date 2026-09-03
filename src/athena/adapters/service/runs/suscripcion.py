"""Runs vivos y quien los esta mirando.

El buffer de repeticion existe para que un cliente que se desconecta pueda
reengancharse sin perder eventos ni recibirlos dos veces.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field

from athena.adapters.service.approvals import (
    RemotePermissionPrompt,
)
from athena.adapters.service.runs.opciones import _REPLAY_BUFFER_SIZE, RunOptions
from athena.agent_loop import AgentRunResult
from athena.cancellation import CancellationSource
from athena.events import RuntimeEvent
from athena.goals import GoalBoard
from athena.types import JSONObject
from athena.workspace import Workspace


@dataclass(slots=True)
class Subscriber:
    subscriber_id: str
    run_id: str
    queue: asyncio.Queue[RuntimeEvent | None]
    #: Exactly one subscriber per run may send intents.
    controls: bool = False
    dropped: int = 0


@dataclass(slots=True)
class LiveRun:
    run_id: str
    workspace: Workspace
    options: RunOptions
    cancellation: CancellationSource
    task: asyncio.Task[AgentRunResult] | None = None
    prompt: RemotePermissionPrompt | None = None
    subscribers: dict[str, Subscriber] = field(default_factory=dict)
    controller_id: str | None = None
    #: Cómo se decidió ejecutar este run, tal y como se anunció.
    #:
    #: Guardado además de publicado porque se decide antes de que nadie pueda suscribirse:
    #: un cliente que sólo escuchase el flujo no lo vería nunca, y es justo lo que quiere
    #: saber quien pregunta por qué su objetivo no se planificó.
    shape: JSONObject | None = None
    #: El encargo vigente y su historia. Vive en el run y no en el bucle porque quien lo
    #: revisa habla con el servicio, no con el bucle, y el bucle puede estar dentro de una
    #: llamada al modelo cuando llega el cambio.
    goal: GoalBoard | None = None
    #: The tail of this run's event stream, newest last. Ordering here is the ordering the
    #: bus published in, which is what makes "preserve order per run" a property of the
    #: transport rather than a hope about scheduling.
    recent: deque[RuntimeEvent] = field(default_factory=lambda: deque(maxlen=_REPLAY_BUFFER_SIZE))

    @property
    def finished(self) -> bool:
        return self.task is not None and self.task.done()

    def replay_after(self, event_id: str) -> tuple[RuntimeEvent, ...] | None:
        """Events this run published after `event_id`, or `None` if it is too old.

        `None` and `()` mean different things and the caller must tell them apart: an empty
        tuple is "you are up to date", `None` is "that id fell out of the window, resync".
        Collapsing them would silently let a client believe it had missed nothing.
        """
        buffered = tuple(self.recent)
        for index, event in enumerate(buffered):
            if event.event_id == event_id:
                return buffered[index + 1 :]
        return None
