# Clinical Copilot — Intelligence Layer Design

**Status:** Phase 3 — implemented in [services/copilot](../../services/copilot/README.md)
**Depends on:** [02-rag-hybrid-retrieval.md](02-rag-hybrid-retrieval.md) (retrieval, treated here as a solved subsystem), [03-knowledge-graph.md](03-knowledge-graph.md), [04-conversational-ai.md](04-conversational-ai.md)
**Decisions:** [ADR-0008](../design/adr-0008-copilot-module-boundaries.md) … [ADR-0012](../design/adr-0012-language-model-seam.md)

## 0. The premise

Phase 2 ends at a rendered prompt. Everything between "a clinician asks a question" and "a
clinician can act on the answer" is this phase.

The distinction that shapes the whole design: **an LLM is one component of a clinical
assistant, not the assistant.** In a chatbot the model does the reasoning, decides what to
look up, and decides what to say. In a regulated clinical setting that is exactly the wrong
allocation of responsibility, because none of those decisions are then reviewable. Here the
model is confined to natural-language *generation and extraction*, while planning, tool
selection, evidence aggregation, contradiction detection, safety classification, and answer
validation are deterministic code with tests.

That allocation is not stylistic. FDA's January 2026 clinical-decision-support guidance keeps
its evaluation focused on **whether the healthcare professional can understand the basis of
the recommendation**, regardless of whether the recommendation came from ML or any other
method. A system whose reasoning exists only as a model's hidden activations cannot satisfy
that, however good its answers are. A system that emits an explicit evidence set, an explicit
claim-to-evidence mapping, and an explicit trace of what it did can.

## 1. The pipeline

```
                 ┌──────────────────────────────────────────────┐
  clinician ───► │ 0. admit      safety pre-checks, tenant scope │
                 ├──────────────────────────────────────────────┤
                 │ 1. remember   working + episodic + semantic   │
                 │               memory → resolved question      │
                 ├──────────────────────────────────────────────┤
                 │ 2. plan       question + memory → typed Plan  │
                 ├──────────────────────────────────────────────┤
                 │ 3. execute    retrieval ∥ graph ∥ tools       │
                 │               (concurrent, failure-isolated)  │
                 ├──────────────────────────────────────────────┤
                 │ 4. aggregate  one evidence set, deduplicated, │
                 │               provenance preserved            │
                 ├──────────────────────────────────────────────┤
                 │ 5. reason     evidence → supported claims     │
                 ├──────────────────────────────────────────────┤
                 │ 6. generate   claims → prose (the only stage  │
                 │               that needs a language model)    │
                 ├──────────────────────────────────────────────┤
                 │ 7. reflect    verify every claim against the  │
                 │               evidence that is supposed to    │
                 │               support it                      │
                 ├──────────────────────────────────────────────┤
                 │ 8. validate   safety gate + confidence gate   │
                 └──────────────────────────────────────────────┘
                                       │
                    confident? ────────┴──────── not confident?
                 answer + explanation           uncertainty response
```

Each stage is a pure function of `(CopilotState) → CopilotState`. State is a frozen
dataclass; a stage returns a new one. Three properties follow directly:

- **Every stage is independently testable** with a hand-built state and no I/O.
- **Every stage is independently observable** — the state carries the trace, so "why did it
  answer that" is answered by reading a value, not by re-running with logging on.
- **The pipeline is checkpointable.** State is serialisable at every boundary, which is what
  makes human-in-the-loop pause/resume possible without a workflow engine (§6).

## 2. Planning: deterministic first, model second

Research on plan-and-execute is consistent that separating planning from execution improves
long-horizon coherence — the planner reasons about the whole task while executors do not
decide what comes next. It is equally consistent that reflection and planning loops are where
latency and cost go, and where a bad self-assessment compounds.

So the planner is a **rule-based classifier over clinical question shapes** that emits a typed
`Plan`: an ordered list of `PlanStep`s, each naming a capability and its arguments. The
question shapes are a small, stable set (Phase 2's `QueryIntent` already enumerates them), the
rules are inspectable and free, and a plan is a value that can be asserted on in a test.

`Planner` is a protocol. An LLM planner implements it later for the open-ended tail. The
important property is that *whatever* produces the plan, the plan itself is data that the
executor validates before running: unknown capability, malformed arguments, or a step count
over budget are rejected before any tool executes.

## 3. Tools

A tool is a named capability with a JSON-Schema argument contract, a declared side-effect
class, and a declared PHI class. Ten ship in Phase 3 (patient lookup, diagnosis lookup,
medication lookup, graph traversal, lab trend analysis, document search, timeline
reconstruction, drug-interaction check, guideline lookup, risk score).

Three properties are enforced by the registry rather than by convention:

**Arguments are validated against the schema before execution.** A tool never defends itself
against malformed input; that is one place, tested once.

**Every tool declares whether it reads PHI.** The executor requires the caller's scopes to
cover a tool's declared class, so a tool cannot widen the caller's access by being called.

**Every tool returns evidence, not prose.** A tool result is a typed record with provenance,
so the aggregator can treat a lab value from a tool exactly as it treats a chunk from
retrieval — and a claim can cite it.

## 4. Reasoning and reflection

The reasoning stage turns an evidence set into **claims**. A claim is a statement plus the
evidence ids that support it plus a support strength. Nothing downstream is allowed to assert
anything that is not a claim.

Reflection then verifies each claim *against the evidence it names*. This is deliberately not
an LLM critiquing its own output. The research literature is explicit that purely internal
self-critique can reinforce an error rather than catch it, and that tool-interactive
verification (the CRITIC pattern) is what makes reflection reliable. Here the "tool" is the
evidence set itself:

| Check | What it catches |
|---|---|
| citation resolvability | a citation index with no corresponding evidence |
| lexical support | a claim whose content does not appear in the evidence it cites |
| numeric fidelity | a number in the answer that appears in no cited evidence |
| contradiction | two pieces of cited evidence asserting incompatible values |
| coverage | a question facet no evidence addresses |

A claim failing verification is dropped, not rewritten. Rewriting is where a reflection loop
becomes a generator of plausible-sounding corrections; dropping is auditable.

## 5. Confidence, and what to do without it

Confidence is computed from named components — evidence strength, agreement, coverage,
recency, source quality, and verification outcome — and every component is reported. A single
opaque number would be worse than none, because it invites reliance it has not earned.

Below the configured threshold the copilot answers with an **uncertainty response**: what it
found, what is missing, and what would resolve it. That path is a first-class output type, not
an error, because "I do not have enough to answer" is the correct clinical answer far more
often than a hedge.

## 6. Human-in-the-loop

Any step whose tool declares `requires_approval` suspends the run and returns a
`PendingApproval` carrying the serialised state. Resuming replays from that checkpoint with
the decision recorded in the trace. This is the LangGraph checkpointing pattern — persist
state at node boundaries so a run can pause, ask, and resume — implemented against our own
state type rather than by adopting the framework ([ADR-0012](../design/adr-0012-language-model-seam.md)).

## 7. Safety

Safety runs twice: on admission (is this question answerable at all — scope, PHI, obvious
harm) and on the assembled answer (does the evidence justify what is about to be said).

Five detectors, each producing typed findings rather than a boolean:

- **insufficient evidence** — nothing, or nothing above the relevance floor
- **contradiction** — cited sources disagree on a value or an assertion
- **staleness** — the evidence answering a time-sensitive question is old
- **ambiguity** — a term in the question resolves to several distinct clinical concepts
- **dangerous combination** — a medication pair with a known interaction in the graph

A finding at `BLOCK` severity suppresses the answer and returns the safety response. Findings
below that are attached to the answer, because a clinician reading a caveat is better served
than one reading nothing.

## 8. Output

One answer object, four renderings: Markdown (clinician-facing), JSON (API), FHIR
`DocumentReference` + `Provenance` (record integration), and a compact API envelope. Rendering
never re-derives anything — every renderer is a pure projection of the same validated answer,
so two surfaces cannot disagree.

## 9. What this phase does not do

No model is called. `LanguageModel` is a protocol with a deterministic, extractive
implementation that composes answers from the evidence it is given. That keeps the entire
platform runnable, testable, and benchmarkable with no inference endpoint, and it makes the
seam explicit rather than assumed. It also means **the generation quality figures in this
phase measure the platform, not a clinical assistant** — see the Phase 3 report.
