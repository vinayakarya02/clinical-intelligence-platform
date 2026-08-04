# ADR-0012: A LanguageModel protocol, and no agent-framework dependency

**Status:** Accepted (Phase 3)

## Context

Two adoption decisions present themselves: which agent framework (LangGraph, an SDK agent
loop, or none), and which model provider.

The framework question is not primarily about quality. LangGraph's central mechanism —
persist typed state at node boundaries so a run can pause, ask a human, and resume — is a
pattern, and this pipeline already has typed immutable state at every stage boundary.
Adopting the framework would mean expressing our stages as its nodes, our state as its schema,
and our checkpoints in its store, in exchange for a graph runner we do not need because our
control flow is deliberately fixed ([ADR-0009](adr-0009-deterministic-orchestration.md)).

The provider question is constrained: no inference endpoint is available in this environment,
and a clinical deployment additionally requires the provider to be BAA-covered, which is a
per-tenant procurement fact rather than a code fact.

## Decision

No agent framework. The HITL checkpoint pattern is implemented directly against
`CopilotState`, which is serialisable by construction.

One protocol, `LanguageModel`, with `complete()` and `extract()`, returning token usage on
every call. Two implementations ship:

- `ExtractiveLanguageModel` — deterministic, composes answers from the evidence it is given
  and extracts values by pattern. Not a mock: it produces real, evidence-grounded output, and
  it *cannot* hallucinate, which makes it a useful lower bound on the safety properties.
- `NullLanguageModel` — refuses, for testing the no-model path.

Every call site records `model_key`, prompt version, and token usage, so switching to a real
provider changes cost and quality but not the trace shape.

## Consequences

- The whole platform runs, tests, and benchmarks with no API key.
- Generation quality is bounded by the extractive model. Fluency figures from this phase say
  nothing about a real model, and the report says so.
- Any streaming interface is future work; the protocol returns a complete response today.
- If a genuinely graph-shaped workflow appears later, this decision should be revisited
  rather than worked around.
