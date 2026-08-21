# ADR-024: Execution mode is asked for; the shape is reported

- Status: **Accepted** — implemented 2026-08-21 across `athena.adapters.service.orchestration`, `athena.adapters.service.runs`, `athena.adapters.service.server`, `athena.planning`, `athena.metrics`, `athena.events`, `athena_service`; consumed by ChatyGPT
- Date: 2026-08-21
- Affects: the HTTP contract, the state frame, and what a client can say about a run

## Context

Once the hierarchical layer ran from the service, a client had exactly one thing to say
about it: `hierarchical: true | false | null`. Three states in a boolean, and the third one
unreadable — `null` means "unset", which nobody can tell apart from "not supported" or
"left at the default", and the difference decides whether a run is planned at all.

Worse, the runtime had two ways to end up running a loop that looked identical from
outside: the evidence said a graph was not worth it, or the deployment had no planner. A
client measuring the planning layer could not tell which had happened.

## Decision

**`execution_mode` names the three things, and the run reports which one happened.**

| Mode | Meaning | Planning unavailable |
| --- | --- | --- |
| `auto` | Athena picks the best strategy available. The default. | Degrade to the loop, and say so |
| `hierarchical` | The graph is a requirement, even for a single task | **400**, before the run exists |
| `direct` | The loop is a requirement; nothing is measured | Irrelevant |

The asymmetry is the point, and it generalises: **a capability that was required fails
loudly, a capability that was preferred falls back quietly and on the record.** Somebody
who asks for a graph is usually measuring one, and a loop reporting itself as that run
would corrupt the measurement rather than fail it. `auto` asks for the best available
strategy, and the loop is a perfectly good answer to that question.

`hierarchical` never falls back. When no usable plan comes back it runs the whole goal as
one task — a truthful plan, in that it says the work was not divided — because a benchmark
that quietly compared the graph against itself half the time would be worse than one that
failed.

### Two questions, two answers, both reported

`DecompositionPolicy` is asked twice and the answers are kept apart:

- `assess(signals)` weighs evidence about a goal *before* anybody has decomposed it. It
  costs a filesystem scan, not a model call, which is what makes `auto` affordable as the
  default;
- `assess_plan(graph)` weighs the decomposition that came back. A model asked to divide
  work will divide it, and the result can be a list of steps rather than a graph.

`TaskGraph.build` is not involved in either. Validity is its question and it already
answers it; a structurally fine plan that buys nothing is not an invalid plan, and putting
a judgement about worth inside a constructor would hide it from everything that wants to
report it.

What a graph buys is **concurrency** and **specialisation**, so those are what
`assess_plan` measures. Node count is not the signal: five tasks in a chain for one
specialist is a to-do list. Dependencies are followed transitively, or a chain of three
would pass for concurrent because its ends do not name each other.

### The shape is state, not only an event

`plan.decided` is published once, when the answer is final — which for a planned goal is
*after* the plan came back. That is before any client can subscribe, so the event alone is
unreachable for the client that most wants it. The registry keeps the shape and the SSE
state frame carries it, which is what any client receives on connecting, first time or
after a reconnection.

A run reports four stable values and two sentences:

| Field | Stable | Meaning |
| --- | --- | --- |
| `execution_mode` | yes | what was asked for |
| `executed_as` | yes | `direct` or `hierarchical` |
| `reason_code` | yes | one of `ShapeReason` |
| `policy_verdict` | yes | `decompose` or `decline` |
| `reason` | no | the effective reason, for people |
| `policy_explanation` | no | what the policy thought, for people |

`reason` and `policy_explanation` will be rewritten; nothing that counts may read them.
And the two are separate because they can disagree — a goal the policy holds worth
decomposing, running on the loop because the deployment has planning off, would otherwise
explain itself with a verdict it never acted on.

## Forward compatibility of the state frame

**Clients ignore fields they do not know, and Athena may add them.** `shape` was added to
the state frame without a `wire_version` bump and without ChatyGPT changing, because that
is what this property is for: a runtime that could not add a field without breaking every
deployed client would be a runtime nobody could improve.

The rule has two halves and both are load-bearing:

- Athena adds fields; it does not repurpose or remove them. A field that must change
  meaning gets a new name, and the old one keeps its old meaning until the wire version
  says otherwise.
- A client deserialises permissively. In ChatyGPT this means `MarcoEstado` does **not**
  declare `deny_unknown_fields`, and `un_marco_con_campos_que_no_conocemos_se_sigue_leyendo`
  in `pruebas_supervisor.rs` fixes it. That test exists to fail if somebody tightens the
  struct, which would otherwise be an invisible change until the next Athena release broke
  the application in the field.

## Consequences

- `ATHENA_PLANNING` defaults to **on**. Deciding costs a repository read, and a planner is
  only asked once the policy has already said decomposing is worth it, so the earlier
  opt-in was charging for something `auto` does not spend.
- A refused `hierarchical` request creates no run at all. It previously left a `LiveRun`
  registered that nothing would execute or close, counted in `/v1/health` until restart.
- `RunMetrics` records `requested_mode`, `selected_shape`, `policy_verdict` and
  `reason_code`, which are the four columns a comparison between strategies groups by.
  They are distinct from `hierarchical`, which is observed from a graph actually starting:
  that one says what happened, these say what was decided, and a run that never got that
  far still has an answer for them.
- ChatyGPT shows the decision as "Estrategia de ejecución", with the policy's opinion
  displayed only when it differs from what was done — which is the case that needs
  explaining.
