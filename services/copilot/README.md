# Clinical Copilot — Intelligence Layer

Phase 3. Everything between "a clinician asks a question" and "a clinician can act on the
answer": conversational memory, planning, tool calling, evidence aggregation, claim
construction, verification, safety, explanation, and four output renderings.

**Scope boundary:** retrieval is a solved subsystem consumed through
[services/retrieval](../retrieval/README.md). This service adds no retrieval logic.

## The shape of the thing

An LLM is one component of a clinical assistant, not the assistant. In a chatbot the model
decides what to look up, reasons, and decides what to say. Here the model does one job —
turning verified claims into prose — and planning, tool selection, aggregation, contradiction
detection, safety classification, and answer validation are deterministic code with tests.

That allocation is not stylistic. FDA's January 2026 clinical-decision-support guidance keeps
its evaluation focused on **whether the healthcare professional can understand the basis of
the recommendation**. A system whose reasoning exists only inside a model cannot satisfy that
however good its answers are.

## Pipeline

```
  question ──► ┌──────────────────────────────────────────────────────┐
               │ 1. remember   working + episodic + semantic memory     │
               │               resolves "his creatinine" to a patient   │
               ├──────────────────────────────────────────────────────┤
               │ 2. plan       question → typed Plan, validated before  │
               │               a single step executes                   │
               ├──────────────────────────────────────────────────────┤
               │ 3. execute    tools concurrently, failure-isolated,    │
               │               suspending for human approval if needed  │
               ├──────────────────────────────────────────────────────┤
               │ 4. aggregate  one deduplicated, ranked evidence set    │
               ├──────────────────────────────────────────────────────┤
               │ 5. reason     evidence → claims (code, not a model)    │
               ├──────────────────────────────────────────────────────┤
               │ 6. reflect    verify each claim against the evidence   │
               │               it cites; drop what fails               │
               ├──────────────────────────────────────────────────────┤
               │ 7. generate   claims → prose (the only model call)     │
               ├──────────────────────────────────────────────────────┤
               │ 8. validate   safety gate, then confidence gate        │
               └──────────────────────────────────────────────────────┘
                                       │
            answer ── uncertain ── blocked ── clarification ── needs-approval
```

Every stage is `async (CopilotState, StageDeps) -> CopilotState` over a frozen state. Three
things follow: each is testable with a hand-built state, each is observable because the state
carries the trace, and the run is checkpointable — which is how human-in-the-loop suspension
works without a workflow engine.

## What cannot happen, structurally

**An unexplained conclusion.** `Claim` cannot be constructed without evidence ids, and only
claims may be asserted. It is a type constraint, not a prompt instruction.

**A fabricated number.** Verification compares every number in a claim against its cited
evidence, exact-match. 5.4 and 5.6 are similar strings and different facts.

**A guessed patient.** An unresolved pronoun produces a clarification request, never a plan
against a guess.

**A tool widening its caller's access.** Each tool declares a PHI class; the registry checks
the caller holds the matching scope before it runs.

**A prompt built inline.** Task prompts are rendered from the versioned registry, so claim
text — which contains verbatim passages from user-uploaded documents — always meets the model
inside an injection boundary, and every answer records the versions that produced it.

## Modules

Dependency direction is enforced by a test, not by convention
([ADR-0008](../../docs/design/adr-0008-copilot-module-boundaries.md)).

| Layer | Modules | Responsibility |
|---|---|---|
| 0 | `domain`, `records`, `textutil` | Value objects, FHIR-shaped clinical records, shared text primitives |
| 1 | `llm`, `prompts`, `memory`, `timeline` | Capabilities over layer 0 |
| 2 | `tools` | Ten clinical capabilities behind one registry |
| 3 | `planner`, `reasoning`, `validation`, `safety`, `explanations`, `output`, `evaluation` | Stage logic |
| 4 | `agents` | The stages themselves |
| 5 | `orchestrator` | Sequencing; nothing depends on it |

## Tools

`patient_lookup` · `diagnosis_lookup` · `medication_lookup` · `lab_trend` ·
`timeline_reconstruct` · `risk_score` · `guideline_lookup` · `drug_interaction_check` ·
`graph_traversal` · `document_search`

A tool whose backing service is absent is *not registered*, so the planner never builds a plan
around a capability that will certainly fail.

## Running it

```bash
python -m cip_copilot.demo
```

Five conversations across a realistic tenant, then an evaluation pass and benchmarks. See the
[Phase 3 engineering report](../../docs/design/phase-3-engineering-report.md).

## What is deliberately not real yet

**`ExtractiveLanguageModel` is not a mock and not a clinical model.** It implements the full
`LanguageModel` contract and composes genuine, cited, evidence-grounded answers — by selection
and templating rather than generation. It *cannot* hallucinate, which makes it a useful lower
bound on the safety pipeline: several validation bugs were found precisely because it flagged
things that could not have been wrong. It is not fluent, and fluency numbers measured against
it say nothing about a real provider.

**The knowledge graph is consulted, not built here.** Graph coverage in production is whatever
the entity-extraction stage (Phase 2 remainder) wrote.
