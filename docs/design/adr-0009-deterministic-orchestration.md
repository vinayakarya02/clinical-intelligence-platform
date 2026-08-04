# ADR-0009: Deterministic orchestration, not an LLM agent loop

**Status:** Accepted (Phase 3)

## Context

The dominant agent pattern is a model in a loop with tools: the model decides what to call,
reads the result, and decides again until it decides to answer. It is flexible and it is what
most frameworks optimise for.

It also has properties that are disqualifying in a regulated clinical setting:

- **Non-determinism at the control-flow level.** The same question can take different paths on
  different runs. "Why did it look that up?" has no stable answer.
- **Unbounded cost and latency.** The loop decides when to stop.
- **Untestable branching.** A branch taken by a model cannot be asserted on without the model.
- **Auditability.** The decision to call a tool that reads PHI is made inside a model.

Research on plan-and-execute separation reaches a related conclusion from the quality side: a
planner that commits to a plan up front produces better long-horizon coherence than a model
choosing one step at a time.

## Decision

Control flow is code. A `Planner` produces a typed `Plan` before anything executes; an
executor validates and runs it; the sequence of stages is fixed.

The model's job is confined to language: extracting a value from a passage, composing prose
from claims. Those are the tasks it is uniquely good at and where a wrong answer is caught by
the verification stage, because the output is checked against the evidence it came from.

`Planner` is a protocol precisely so an LLM planner can be added for the open-ended tail — but
its output is still a `Plan` that is validated before execution, so the safety and cost
properties hold regardless of what produced it.

## Consequences

- Question shapes outside the rule set get the broad default plan rather than a clever one.
  Measured by planner accuracy in the evaluation harness, so the gap is visible.
- Every path through the system is reachable in a test without a model.
- Adding a capability means adding a tool and a planning rule — two small, reviewable diffs —
  rather than adjusting a prompt and hoping.
