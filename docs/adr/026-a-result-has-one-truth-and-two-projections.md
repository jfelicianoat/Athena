# ADR-026: A result has one truth and two projections

- Status: **Accepted** — implemented 2026-08-22 in `athena.schema`, `athena.tool_projection`, `athena.tools`, `athena.tool_executor`, `athena.agent_loop`, and across every tool Athena owns
- Date: 2026-08-22
- Extends: ADR-003 (tools are capability contracts) — this is the half of that contract that was never enforced
- Affects: what a tool must return, what the model is told, and what a client draws

## Context

ADR-003 said every tool declares an output schema. Every tool did. Nothing ever checked
one, and it turned out nothing could have: **all of them declared `{"type": "object"}`** —
a contract that says nothing and therefore cannot be broken. The declaration was
decorative, and the longer it stayed that way the more everything downstream quietly came
to depend on fields nobody was verifying still existed.

Two more things followed from having only `ToolResult.output`:

- it went to the model verbatim, so a directory listing of a hundred files cost a hundred
  files' worth of JSON in a context window that is the scarcest thing in a run;
- ChatyGPT re-derived presentation by reading event payloads, which couples a client to an
  internal format and makes every future client repeat the work and disagree in detail.

## Decision

**One canonical result, checked against a contract that says something, and two projections
derived from it.**

The ordering rule is the whole decision: **projections derive from the canonical result,
and nothing derives the canonical result from a projection.** What is trimmed for the model
is not trimmed from the record. What is prettied for an interface does not change what was
verified. A tool may replace either projection — it knows what it returns better than a
general case does — but it cannot change through that door what it claims to have done.

### The contract obliges by default

`OutputContract.ENFORCED` is the default, and the failure of a tool to return what it
declared is a `ToolContractError` — an *execution* error, not a validation one. The
arguments were fine; the tool broke its word. The distinction is load-bearing for recovery:
recovering from a bad argument means reformulating the call, and reformulating this one
would fix nothing.

`OutputContract.DECLARED` exists for exactly one situation: results Athena does not
produce. An MCP tool's output comes from a remote server, so Athena cannot require it —
imposing would turn somebody else's version bump into our outage. The deviation is
published as `tool.contract.violated` instead of imposed, because tolerating it silently
would let a deviation become that tool's normal shape without anyone deciding so.

Enforcement happens **before** externalisation. Afterwards what exists is the store's
receipt, not what the tool promised, and checking the receipt against the result's schema
would fail every large result — the worst way to be right.

### The checker says what it does not check

`athena.schema` is not a JSON Schema implementation and does not pretend to be one. Athena
has no dependencies. It checks type, required, unknown fields, element types and `enum`,
and **silently ignores every keyword it does not implement**. A checker that failed on an
unimplemented keyword would reject valid contracts; one that pretended to understand them
would report as verified what it never looked at. The module says so at the top, because a
partial checker whose limits are undocumented is worse than no checker.

### Two projections, made together

| | For the model | For an interface |
| --- | --- | --- |
| Type | `ModelView` | `DisplayView` |
| Carries | text, whether it was trimmed, where the rest is | kind, title, summary, items, facts |
| Sized by | context cost | what fits on a screen |

They are produced together by one `project()` rather than by two independent methods,
because a tool that knows how to explain itself to a model knows what to show, and two
methods drift into telling two stories.

`ResultKind` has five values — text, items, change, record, reference — chosen because
they change how something is drawn. A kind per tool would be a catalogue that grows with
every tool and tells whoever draws nothing new.

The default projection infers shape from the result's *structure*, never from the tool's
name: a name is chosen by whoever writes the tool and would change the presentation on a
rename. Where it genuinely cannot tell — `bash` returns both `stdout` and `stderr`, and
which one is "the body" is not a general question — it declines to choose rather than pick
the first and hide a traceback behind an empty success.

`grep` and `read_range` override the default, and this is what the seam is for: `grep`
renders `file:line: text` and `read_range` renders numbered source, which costs fewer
tokens than `path=… line=… text=…` and lets the model cite a line by number — the reason
those tools exist.

The views travel in `ToolResult.metadata`, not in `output`: putting a view inside `output`
would leave the result failing its own schema.

## Amendment (2026-08-22): a tool also declares how long it may take

`ToolSpec.timeout_seconds` was added under ADR-030. A single executor-wide ceiling is a
policy that cannot be true for every tool: a file read taking thirty seconds is broken, and
a delegation that only gets thirty seconds never starts. It belongs with the rest of the
contract for the same reason the output schema does — the tool knows, and the executor was
guessing.

## Consequences

- Every tool Athena owns now declares what it actually returns, and
  `tests/test_tool_output_schemas.py` executes each one and compares. That test exists
  because a **real run against the broker** found what 835 unit tests had not:
  `read_range` declared a list of strings and returns a list of objects. No test caught it
  because every test asked what the tool does, and none asked whether that is what it said
  it would do.
- A tool added without an entry in that test fails the suite. Being awkward to construct is
  not an exemption — `bash` and `tool_search` are in it — because a list of the easy tools
  is how the gaps stay.
- The model receives the projection when one exists and the raw output when it does not. A
  `ToolResult` built by hand has never crossed the executor, so `model_view_of` returns
  `None` rather than fabricating a view that would disguise a skipped contract.
- `tool.completed` carries the display view, so a client draws from a derived, stable shape
  instead of reverse-engineering an internal payload. ChatyGPT can stop deriving its own;
  nothing forces it to yet, since unknown fields are ignored by contract (ADR-024).
- `git_commit` returns `output`, a field name that now collides confusingly with
  `ToolResult.output`. It is left alone: renaming it is a wire change for a cosmetic gain.
