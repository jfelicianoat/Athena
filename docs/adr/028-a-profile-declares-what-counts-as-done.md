# ADR-028: A profile declares what counts as done

- Status: **Accepted** — implemented 2026-08-22 in `athena.profiles`, `athena.verification` (`ArtifactVerificationPolicy`), `athena.context`, `athena.adapters.service.runs`, `athena.adapters.service.server`
- Date: 2026-08-22
- Relates to: ADR-012 (verification owns completion), ADR-015 (three delegates, not a swarm), ADR-027 (not verified is not verified wrong)
- Affects: which tools a run has, how it is verified, and the words it is given to think in

## Context

Athena was written for software repositories and said so everywhere. The system prompt
opened with "a coding agent working in a repository". Verification discovered commands from
`pyproject.toml` and `package.json`. And after ADR-027, a domain with no executable checks
ended every run with `verification_inconclusive` — correct as a diagnosis, and as the only
available ending, a polite way of saying Athena is for code and nothing else.

The audit predicted this exactly: *"Phase 8's non-developer profile is the real test of the
core."* The point of a non-developer profile is not to serve a second audience. It is the
only way to find out whether the runtime was ever general, or whether the generality was
just untested.

## Decision

**An `AthenaProfile` declares what kind of work a run is, and therefore what would prove it
was done.** Four things vary and nothing else does:

| | What it decides |
| --- | --- |
| `subject` | the noun the prompt uses — "a repository", "a collection of documents" |
| `tools` | which tools **exist** for the run, not which are permitted |
| `evidence` | executed checks, or produced artifacts |
| `proves` | what that evidence establishes — **including what it does not** |

The fourth is what stops this becoming the place where runs get approved without being
checked. A profile may declare weaker evidence. What it may not do is keep quiet about it.
`DOCUMENTS.proves` ends with "It does not establish that their content is correct", that
sentence travels with the result, and a test asserts it is there.

### This is not `SubagentProfile`

The word collides and the concepts do not. ADR-015 defines three **role** profiles —
Explorer, Coder, Verifier — which divide authority *inside* a run. This is the layer above:
what kind of work the whole run is. A documents run can still delegate to a Coder. The two
compose; they do not merge, and ADR-015 has been amended to say so, because the next person
to read both would otherwise have to work it out.

### Tools: two filters, in this order

The profile decides what **exists**; the run's capabilities decide which of those are
**granted**. Profile first, structurally — the same principle as `registry_for()`: a tool
outside the profile is not in the catalogue, so refusing it does not depend on a policy
being configured right. The other order would give a documents run a shell as soon as
somebody passed `exec=allow`, and a non-developer profile that kept `bash` would prove
nothing at all — it could still run the test suite, and the coupling would survive
unnoticed.

### Evidence without a command runner

`ArtifactVerificationPolicy` proves that the declared deliverables **exist, are non-empty
and were written by this run**. Deterministic, produced by the runtime, and independent of
the model claiming to be finished — which is what ADR-012 requires. Three ways of cheating
it are closed and tested: an empty file is not a deliverable, a file that was already there
was not produced by this run, and a run that wrote nothing at all is `INCONCLUSIVE` rather
than failed, because there is nothing to show either way.

A client may name the deliverables, and should when it can. Without them the policy checks
what the run *says* it wrote, which is weaker and is reported as weaker. That path is
exposed over HTTP on purpose: a policy only the test suite can reach is a policy that does
not exist in production.

### Verification is chosen in one place

`RunRegistry.verification_for()` is the only thing that decides how a run is verified. The
direct, hierarchical and resumed paths each built their own before, so a new profile would
have reached one and not the others — and the same run would have been verified differently
depending on which door it came in through.

## Consequences

- **The core was general; nothing had checked.** A full run against the real broker on a
  folder of meeting notes — no `pyproject.toml`, no git, no shell, nothing to execute —
  went glob → read_file → write_file → verification passed → completed, and produced a
  usable report. That run is the phase's actual result; `tests/test_profiles.py` is the
  version of it that runs every time.
- `GET /v1/profiles` reports what a deployment offers, including each profile's `proves`.
  Choosing blind between profiles that change which tools exist is not choosing.
- An unknown profile is a **400 before the run is created**, never a fall back to the
  default. Whoever asked for `documents` and silently got software finds out when Athena
  tries to run the test suite of a folder of prose — late, and in the wrong place.
- The default is still `software_engineering`. Introducing profiles must not change what a
  deployment that never asked for them does.
- Verification is now told what the run *did* (`files_modified`, `commands_run`), not only
  the last sentence the model produced — which was the one thing in the session that is not
  evidence.
- `VerificationPlanner` and `RepositoryScout` still reason in software terms. That is
  correct for the profile that uses them and irrelevant to one that does not; the coupling
  the audit warned about turned out to be confined to the policies, not the loop.
