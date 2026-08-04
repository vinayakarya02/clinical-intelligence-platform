# Phase 3 Engineering Report — Clinical Copilot

**Scope delivered:** the intelligence layer over retrieval — multi-turn memory, planning, a
ten-tool calling framework, evidence aggregation, claim construction, deterministic
verification, clinical safety detection, explanation assembly, human-in-the-loop approval,
prompt registry v2, four output renderings, and an evaluation harness.

**Verification status:** 726 of 737 tests pass, 11 skip (integration tests needing live
PostgreSQL/MongoDB/Neo4j, and OCR needing Tesseract). `ruff format`, `ruff check`, and
`pyright` are clean across the repository.

| | |
|---|---|
| Copilot source | 35 modules, 6,456 lines |
| Copilot tests | 122 |
| Repository total | 737 tests — 726 pass, 11 skip |

---

## 1. Architecture summary

The pipeline is `remember → plan → execute → aggregate → reason → reflect → generate →
validate`, each stage an `async (CopilotState, StageDeps) -> CopilotState` over a frozen
state. The full design is [07-clinical-copilot.md](../architecture/07-clinical-copilot.md);
the decisions are ADR-0008 through ADR-0012.

The organising idea: **an LLM is one component of a clinical assistant, not the assistant.**
The model composes prose from verified claims. Planning, tool selection, aggregation,
contradiction detection, safety classification, and answer validation are deterministic code.

That allocation is what makes the system reviewable, and reviewability is the regulatory
requirement — FDA's January 2026 CDS guidance evaluates whether the healthcare professional
can understand the *basis* of a recommendation, regardless of the method that produced it.

Four properties are enforced by types and tests rather than by prompt instructions:

- A `Claim` cannot be constructed without evidence ids, and only claims may be asserted.
- Every number in a claim is checked against its cited evidence, exact-match.
- An unresolved pronoun produces a clarification request, never a plan against a guess.
- A tool cannot widen its caller's access; the registry checks scopes before it runs.

## 2. Components implemented

| Objective | Where | Note |
|---|---|---|
| Multi-turn conversation | `memory/` | Three tiers; memory resolves references, never supplies evidence |
| Clinical reasoning | `reasoning/` | Evidence → claims, in code |
| Explainable AI | `explanations/` | Evidence, graph chains, trace, decomposed confidence, uncertainty |
| Tool calling | `tools/` | Ten tools, JSON-Schema args, PHI classes, approval gates |
| Agentic workflow | `agents/`, `orchestrator.py` | Eight stages, fixed order, each independently testable |
| Reflection | `validation/` | Deterministic verification; failing claims dropped, never rewritten |
| Clinical safety | `safety/` | Five detectors, typed findings, severity-driven handling |
| Structured responses | `output/` | Markdown, JSON, API envelope, FHIR Bundle |
| Timeline intelligence | `timeline/` | Four tracks, clinical same-day precedence, undated records surfaced |
| Explainable graph | `explanations/` | Chains (`A → [causes] B`), not node lists |
| Prompt registry v2 | `prompts/` | Deployment pins, rollback, session-stable experiments |
| Evaluation | `evaluation/` | Planner recall, verification, hallucination, abstention, tokens, cost |

## 3. Engineering decisions

**Deterministic orchestration over an LLM agent loop** ([ADR-0009](adr-0009-deterministic-orchestration.md)).
A model in a loop with tools is non-deterministic at the control-flow level, unbounded in
cost, untestable at its branches, and makes the decision to read PHI inside a model. Control
flow is code; `Planner` remains a protocol so an LLM planner can be added, but its output is
still a validated `Plan`.

**Verification, not self-critique** ([ADR-0010](adr-0010-verification-not-self-critique.md)).
The research on reflection is clear that internal self-critique can reinforce an error, and
that tool-interactive verification is what makes it reliable. The "tool" here is the evidence
set. Failing claims are *dropped*, never rewritten — rewriting generates plausible corrections
that are themselves unverified.

**One distribution, enforced boundaries** ([ADR-0008](adr-0008-copilot-module-boundaries.md)).
Twelve packages would share domain types, so either a thirteenth appears or they import each
other. Twelve internal modules with a test walking the import graph gives the modularity
without the packaging. That test earned its keep immediately (§5).

**Three memory tiers with one rule** ([ADR-0011](adr-0011-memory-tiers.md)): memory resolves
references, never supplies evidence. Summaries are lossy, and a lossy lab value is a wrong lab
value.

**No agent framework** ([ADR-0012](adr-0012-language-model-seam.md)). LangGraph's central
mechanism — persist typed state at node boundaries to enable pause/resume — is a pattern this
pipeline already has. Adopting it would mean expressing our stages as its nodes in exchange
for a graph runner we do not need, because our control flow is deliberately fixed.

## 4. Research findings applied

| Finding | Applied as |
|---|---|
| FDA Jan 2026 CDS guidance evaluates whether the clinician can understand the *basis* | Explanation is a pipeline stage, not a rendering option |
| Plan-and-execute improves long-horizon coherence; executors shouldn't choose next steps | `Plan` is committed and validated before execution |
| Reflection loops add latency and can compound errors; CRITIC-style external verification is what works | One deterministic verification pass against the evidence set |
| Agent memory splits into working / episodic / semantic | Exactly those three tiers, with explicit promotion rules |
| LangGraph checkpointing at node boundaries enables HITL | Serialisable `CopilotState`; suspend returns it, resume replays |
| Copilot layers orchestrator + grounding + responsible-AI on input *and* output | Safety runs on admission and on the assembled answer |
| Strict JSON-schema is the production default for tool arguments | Registry validates every argument before execution |

## 5. Bugs found

All found by running the system, not by unit tests written alongside it.

### Blockers

**B1 — Citation markers were parsed as fabricated clinical values.** `verify_answer_text`
extracted numbers from the rendered answer, so `[1]` became the number 1, which appeared in no
evidence. **Every answer that cited its sources failed verification.** The system could not
answer anything it could also cite — and because the failure surfaced as a low-confidence
"uncertain" response, it looked like cautious behaviour rather than a defect.

**B2 — Interaction questions returned "no evidence".** `drug_interaction_check` could only
check medications some *other* step had already looked up. "Do lisinopril and spironolactone
interact?" — the plainest form of the most safety-critical question this system answers —
planned the step, found zero medications, skipped it, and blocked on no evidence.

### High

**H1 — The task prompt was a hand-built f-string.** It bypassed the versioned registry
entirely: no injection boundary around claim text (which contains verbatim passages from
user-uploaded documents), and `prompt_versions` recorded only the system and developer layers,
so an answer-quality regression could not be attributed to the prompt that caused it.

**H2 — The human-in-the-loop path was unreachable dead code.** No tool set
`requires_approval`, so the suspension branch, `PendingApproval`, `resume()`, and the
`NEEDS_APPROVAL` and `approval_denied` modes had never executed. An untested safety mechanism
is worse than an absent one, because it will be relied upon.

**H3 — `MemoryStore` was unbounded.** One entry per session, forever: an OOM in a long-running
process, and PHI-adjacent conversational content retained for the life of the process under no
policy anybody declared.

**H4 — A genuine import cycle between `tools` and `timeline`,** plus `safety` importing a
*private* regex out of `validation` and `validation` importing a number parser out of
`reasoning`. Found by the ADR-0008 boundary test on its first run — against my own code,
which is the point of writing it.

**H5 — Corroboration claims were generated only to be rejected.** "The value 5.4 is
corroborated by two independent kinds of source" is a fact about the evidence set, not about
the patient, so its words appear in no source and the verifier correctly rejected every one.
Eight of fifteen claims per turn were manufactured to be discarded, dragging the verification
score — and therefore confidence — down with them.

**H6 — A lab-trend summary contradicted itself.** The contradiction detector compared a
computed trend summary (which carries both endpoints of a series, stamped with the latest
date) against the very observations it was derived from, reporting "sources disagree on
potassium" on *every* trend question.

### Medium

**M1 — Coverage scored phrasing, not answering.** Question tokens kept their punctuation
(`medications?` matched nothing) and structural words were counted as uncovered, so
naturally-worded questions scored badly however well they were answered.

**M2 — A truncated answer was recorded and then returned.** `was_truncated` was written to the
trace and ignored; a clinical answer cut off can end just before a caveat and read as complete.

**M3 — `citation_rate` scored a correctly-blocked turn as 0.0,** making working abstention
look like a citation defect in the aggregate.

## 6. Bugs fixed

All of the above. Each carries a regression test:

- citation markers stripped before numeric checking → `test_citation_markers_are_not_read_as_clinical_values`
- interaction entity-resolution moved into the tool, against the graph → verified end-to-end
- task prompt rendered from the registry with a claims delimiter → `TestTaskPromptIsVersioned`
- an approval-gated tool exercising suspend/resume/deny → `TestHumanInTheLoop` (5 tests)
- LRU-bounded memory store → `TestMemoryBounds`
- `records` and `textutil` extracted to layer 0; layer map asserted → `TestModuleBoundaries`
- corroboration reported, not asserted → `test_corroboration_is_reported_not_asserted`
- derived evidence excluded from contradiction detection → `test_a_series_inside_one_item_is_not_a_contradiction`
- coverage tokenised, stopworded, and credited to capabilities run
- truncated generation halts with an uncertainty response
- undefined metrics skipped rather than scored zero

## 7. Benchmarks

In-process, deterministic extractive model, Windows 11 / Python 3.11.

| Stage | Time |
|---|---|
| copilot construction | 83.9 ms |
| multi-tool clinical question | 13.6 ms |
| memory follow-up | 13.6 ms |
| unresolvable reference (clarify) | 2.3 ms |
| no-evidence question (block) | 5.0 ms |
| timeline reconstruction | 27.3 ms |
| evaluation, 6 cases | 45.8 ms |
| peak memory | 0.32 MB |

Per-turn latency p50 **7.0 ms**, p95 12.0 ms. Mean 860 tokens per turn (773 prompt).

**These bound the platform's own cost and say nothing about production latency.** A real
provider adds a network round-trip that dominates every number here, and its token cost is the
figure that actually matters — which is why `CostModel` is injected rather than hardcoded.

### Evaluation (6 labelled cases)

| Metric | Value |
|---|---|
| mode_correct | 1.000 |
| planner_recall | 1.000 |
| claim_verification_rate | 1.000 |
| citation_rate | 1.000 |
| hallucination_rate | **0.000** |
| abstention_correct | 1.000 |
| graph_utilisation | 0.080 |

A hallucination rate of zero is **not** evidence of a safe system. The extractive model
cannot hallucinate by construction, so this measures the pipeline's plumbing, not its
resistance to a model that can. The number becomes meaningful only against a real provider.

## 8. Test summary

122 copilot tests across four areas: tool contract and registry mediation (29), reasoning,
verification, and safety (29), pipeline, planning, memory, prompts, output, and boundaries
(64). Repository total 737, of which 726 pass and 11 skip.

The load-bearing ones are the boundary test (found a real cycle), the HITL suite (covers
previously-dead code), and the verification regressions (each pins a bug that shipped).

## 9. Production readiness

**Ready:** the architecture, the auditability, the isolation model, and the discipline. Every
answer carries a full stage trace, decomposed confidence, cited evidence with provenance, and
the prompt versions that produced it. Abstention is a first-class outcome and is measured.
Tool authorisation is centralised and tested. Module boundaries are enforced by CI.

**Not ready:**

1. **No real language model has ever run through this.** Generation quality, latency, cost,
   and the actual hallucination rate are all unmeasured. Every quality figure above is a
   property of a deterministic composer.
2. **No real infrastructure.** Inherited from Phase 2 and still true: nothing has executed
   against a live Atlas, Neo4j, or PostgreSQL.
3. **Six evaluation cases.** Same criticism as Phase 2's eval set, now compounded — reasoning
   quality is harder to label than retrieval relevance, and needs clinician review.
4. **The rule planner covers the question shapes it was written for.** Planner recall of 1.0
   is measured against cases whose shapes are in the rule set; the open-ended tail is exactly
   what an LLM planner is for, and is not implemented.
5. **`ClinicalDataSource` has one in-memory implementation.** The FHIR/EHR adapter that would
   make this real does not exist.
6. **Interactions are checked when asked, not always.** A clinician asking about potassium on
   a patient taking two interacting drugs is not warned unless the question mentions
   interaction. Whether that should change is a clinical product decision, not an engineering
   one, and it should be made deliberately.

**Assessment:** Phase 3 delivers a production-shaped clinical reasoning platform whose safety
properties are structural rather than prompted, and whose every claim is traceable to evidence
an auditor can follow. The remaining work is substitution and validation, not redesign.

The honest summary of the phase is that **six of the eleven defects were in code that unit
tests passed cleanly** — the citation-marker blocker, the interaction blocker, the dead HITL
path, the self-rejecting claims, the self-contradicting summary, and the import cycle all
required either running the system end to end or asserting a property about the codebase
itself. That is the strongest argument in this report for keeping the demo and the boundary
test as first-class, maintained artifacts rather than scaffolding.

## 10. Technical debt

| Item | Why it matters | Phase |
|---|---|---|
| No real LLM integration | Every quality number is a placeholder's | Next |
| `ClinicalDataSource` has no EHR/FHIR adapter | The structured-data half is demo-only | Next |
| 6-case eval set | Reasoning quality claims rest on it | Before any model selection |
| LLM planner for the open-ended tail | Rule coverage is real but bounded | After eval curation |
| `Answer.safety_findings` is `tuple[Any, ...]` | `SafetyFinding` sits above `domain` in the layer order; the honest fix is a protocol in layer 0 | Cleanup |
| Memory is in-process | A multi-replica deployment needs a shared store, which is a PHI-retention decision | With horizontal scale |
| Streaming responses | The protocol returns a complete response; a chat UI wants tokens | With the UI |
