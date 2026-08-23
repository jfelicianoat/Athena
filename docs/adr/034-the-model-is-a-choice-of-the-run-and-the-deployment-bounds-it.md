# ADR-034: The model is a choice of the run, and the deployment bounds it

- Status: **Accepted** — implemented 2026-08-23 in `athena.model_catalog`, `athena.agent_loop`, `athena.adapters.ai_broker`, `athena.adapters.service.runs`, `athena.adapters.service.server`, `athena_service`
- Date: 2026-08-23
- Extends: ADR-002 (AI_Broker is a model provider) and ADR-028 (a profile declares what counts as done)
- Affects: who decides which model runs a job, and what happens when that model is not available

## Context

The model was a property of the process. `ATHENA_PREFERRED_MODEL` was read at startup and
applied to every run, so no interface could offer a choice and changing model meant
restarting the service. Meanwhile `ModelRequest.model` had been in the contract since H0
and `AiBrokerModelProvider` had always read `request.model or self._preferred_model`: the
seam existed and nothing reached it. This is the same shape as ADR-032 — a capability
complete, argued and unreachable.

It matters more than convenience. Measured against this broker on 2026-08-23, the choice of
model is the difference between a job done and a job invented: on an identical
fix-the-failing-test task, `nemotron-3.5-lightning:30b` fixed the bug in 148 s and
`qwen3-coder:30b` — the model that had been serving real traffic — failed in 38 s having
changed nothing. Of 20 models the broker advertises as tool-capable, 7 could not even
produce one well-formed decision.

## Decision

**The client asks for a model; the deployment decides which models exist.**

This is the same split ADR-004 makes for permissions: the model may request an action and
may not authorise itself. `ModelCatalog` holds the offered names in the order the
deployment wrote them, `ATHENA_ALLOWED_MODELS` fills it, and `ATHENA_PREFERRED_MODEL`
names the default — which is always offered, because a deployment whose active model is
missing from its own list describes itself wrongly.

### An unknown model is a 400, before the run exists

Exactly as for an unknown profile, and refused before `Orchestrator.decide`, which reads
the repository and may spend a model call. Charging for the preparation of a run already
known to be invalid is charging twice for nothing.

### The catalogue is not derived from the broker

The broker advertises 156 models, embeddings and OCR included. Offering all of them would
be offering a list, not a choice — and it would offer models that cannot drive an agent at
all. The list is written by whoever deploys.

### An explicit choice disables the broker's fallback

`AiBrokerModelProvider` sends `fallback_allowed: true` for a deployment *preference* — the
broker routes, and it is the component that knows what is down — and `fallback_allowed:
false` for a run's *choice*. A selector the broker may silently ignore is not a selector,
and somebody who picked a model and got another does not find out until the work comes
back wrong. We would rather the run fail saying that model is unavailable.

### No deployment is forced to offer a choice

Without `ATHENA_ALLOWED_MODELS` and without a preferred model there is no catalogue,
`GET /v1/models` answers 404 with `models_fixed`, and asking for a model is an error
because nobody can grant it. That is the previous behaviour, and it is still correct — the
404 follows the idiom already used by metrics and memory: "this deployment does not do
that" is an answer, not a failure.

## Consequences

- ChatyGPT can offer the picker, and does. A deployment with one model shows no selector:
  a control with a single option asks for a decision that does not exist.
- `POST /v1/runs` accepts `model`; omitting it keeps every existing client working.
- Choosing a model that is down turns a silent substitution into an explicit failure. This
  is a deliberate trade: fewer runs complete, and the ones that do used what was asked for.
- The catalogue must be maintained by hand. That is the cost of not deriving it from a
  list that includes embedding models.

## Alternatives rejected

**Let the client send any name and pass it through to the broker.** Simplest, and it makes
the deployment's own configuration meaningless: any client could route work to a cloud
model that bills, or to one that cannot tool-call, and Athena would have no place to say no.

**Derive the offered list from `GET /api/v1/models` filtered by the broker's `tools` flag.**
Tempting and wrong twice over. Against this broker Athena does not use native tool calling
at all — it sends a JSON schema — so the flag does not predict what it needs; measured, the
flag was true for seven models that could not produce one valid decision.

**Keep the model in the environment and restart to change it.** What we had. It makes the
choice an operator's, and the person who knows whether this job needs a coder model is the
one writing the job.
