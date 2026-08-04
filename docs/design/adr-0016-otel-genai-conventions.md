# ADR-0016: OpenTelemetry GenAI semantic conventions, not a bespoke vocabulary

**Status:** Accepted (Phase 4)

## Context

Phase 4 must track model latency, token usage, cost, tool calls, retrieval timing,
hallucination rate, confidence, and abstention rate. The obvious approach is to name these
metrics after our own domain — `copilot_answer_confidence`, `retrieval_stage_ms` — because
they are our concepts.

The OpenTelemetry GenAI SIG has standardised exactly this surface. `gen_ai.*` covers
operations, providers, request and response models, input and output tokens, tool names and
call ids, conversation ids, embedding dimensions, and — directly relevant here —
`gen_ai.evaluation.name` / `gen_ai.evaluation.score.value` for quality metrics. The
conventions are experimental as of 2026 and carry a documented opt-in for dual emission during
transitions.

## Decision

Use the standard names and attributes wherever one exists. Where none exists — per-request
USD cost, per-stage pipeline durations, cache hit rates — use a clearly-namespaced local name
and mark it in code as a local extension.

Confidence, hallucination rate, groundedness, and abstention are emitted as
`gen_ai.evaluation.*` scores rather than as bespoke gauges, because that is precisely what
those attributes are for and it makes them legible to any conformant backend.

## Consequences

- Telemetry is understood by Grafana, Datadog, and vendor LLM-observability products without a
  translation layer. The dashboards in this repository are one consumer, not the only one.
- The conventions are experimental and will change. Attribute names are centralised in one
  module (`observability/semconv.py`) so a convention bump is one diff, and the module records
  which version it targets.
- Some of our concepts do not fit the vocabulary and are local extensions. Those are marked,
  so a future reader can tell what is standard from what we invented.
