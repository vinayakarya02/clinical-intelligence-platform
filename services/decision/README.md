# Clinical Decision Intelligence

> **The clinical knowledge corpus in this service has not been reviewed by a clinician and must
> not be used in the care of real patients.** It is a demonstration set that exists to exercise
> the engine, and it is convincing enough to be dangerous. Read the
> [clinical safety case](../../docs/safety/clinical-safety-case.md) first.

Phase 5. Everything between "here is what is known about this patient" and "here is a proposed
action a clinician can review": rules, guidelines, drug intelligence, risk stratification, care
pathways, alert suppression, CDS Hooks, and an approval gate.

**Scope boundary:** retrieval (Phase 2) and reasoning (Phase 3) are solved subsystems consumed
through their own entry points. This service adds no retrieval and calls no model.

## Pipeline

```
  patient facts ──► ┌─────────────────────────────────────────────────────┐
                    │ assemble   one PatientContext, no I/O below here    │
                    ├─────────────────────────────────────────────────────┤
                    │ evaluate   active rules → fired, not-fired, unknown │
                    ├─────────────────────────────────────────────────────┤
                    │ check      7 drug checks over the medication list   │
                    ├─────────────────────────────────────────────────────┤
                    │ score      risk models, or a stated refusal to      │
                    ├─────────────────────────────────────────────────────┤
                    │ rank       severity, then evidence quality          │
                    ├─────────────────────────────────────────────────────┤
                    │ detect     contradictions, before suppression       │
                    ├─────────────────────────────────────────────────────┤
                    │ suppress   dedup · override memory · floor · ceiling│
                    ├─────────────────────────────────────────────────────┤
                    │ explain    provenance path into the evidence graph  │
                    ├─────────────────────────────────────────────────────┤
                    │ gate       submit for human review. Nothing acts.   │
                    └─────────────────────────────────────────────────────┘
```

## What cannot happen, structurally

**An unexplained recommendation.** `Recommendation` cannot be constructed without a citation
and a provenance chain. A constructor invariant, not a review checklist item.

**Clinical logic in Python.** Rules, guidelines, interactions, dose limits, risk models, and
pathways are versioned, cited YAML ([ADR-0019](../../docs/design/adr-0019-knowledge-as-data.md)).
A test fails the build if a drug name appears in engine code.

**Arbitrary code from a knowledge file.** Conditions are a typed AST, evaluated by a small
interpreter. There is no `eval` anywhere. A knowledge base is an operator-editable file, and
evaluating one as Python is remote code execution with a clinical veneer.

**"We don't know" silently becoming "no concern".** Evaluation has three outcomes. A rule about
potassium on a patient with no potassium recorded is `unknown` — it never fires, never negates
to true, and feeds the missing-information report.

**A suppressed contraindication.** Exempt from all four suppression mechanisms, and the
evaluation harness counts violations explicitly rather than trusting the exemption.

**An accepted recommendation with no reviewer.** No transition reaches `accepted` without an
identified human, and there is no flag that disables the gate
([ADR-0024](../../docs/design/adr-0024-human-approval-gate.md)).

**A different answer to the same question.** No model participates in a decision
([ADR-0022](../../docs/design/adr-0022-deterministic-decisions.md)). Determinism is what makes
a knowledge base clinically reviewable — a reviewer can reason about what it will do.

## Modules

Dependency direction is enforced by a test. Layer *n* may import only layers below it; a
sideways import fails the build, which is how `factory` ended up where it is.

| Layer | Modules | Responsibility |
|---|---|---|
| 0 | `domain` | Facts, severity, evidence quality, recommendations, provenance |
| 1 | `rules` | The condition language and its evaluator |
| 2 | `knowledge`, `drugs`, `risk`, `pathways` | Loading and the four knowledge-driven engines |
| 3 | `factory`, `contradiction`, `suppression`, `evidence_graph`, `approval`, `hooks`, `smart` | Assembly, cross-cutting logic, external interfaces |
| 4 | `engine` | The pipeline |
| 5 | `workflow`, `evaluation` | Event-driven runs, and measurement |
| 6 | `demo` | The end-to-end run |

## Drug intelligence

Seven checks: drug–drug interaction, drug–allergy, duplicate therapy, drug–condition,
dose ceiling, organ-function adjustment, and drug–age.

Severity and evidence quality are **independent axes** that are never multiplied together
([ADR-0020](../../docs/design/adr-0020-severity-and-evidence.md)). A contraindication documented
only in theory is still shown, and says so.

Allergy cross-reactivity must be **declared** per class. Correct for beta-lactams, wrong for
statins — and the failure direction denies a patient a needed drug on no evidence, so the
default is exact-ingredient matching.

## Alert suppression

Published override rates for clinical decision support run 49–96%, and around 300 reminders are
issued per prevented adverse drug event
([ADR-0021](../../docs/design/adr-0021-alert-fatigue.md)). Suppression is therefore a designed,
measured, audited pipeline stage rather than an afterthought: deduplication by clinical concern,
override memory per patient, a per-role severity floor, and a volume ceiling.

Role tailoring is the mitigation the systematic reviews found most effective. A pharmacist
reviewing a medication list wants moderate interactions; a prescriber mid-order does not, and
showing them trains the reflex that loses the major one.

## Evaluation

`DecisionEvaluator` scores a labelled suite on recall, false positives against **forbidden**
labels, alert burden, and rule coverage — reported side by side, never combined. A run is clean
only if it is also deterministic, suppressed no contraindication, and triggered no forbidden
rule.

Rule coverage earned its place on its first run by naming a corpus rule that no case exercised.

## Running it

```bash
python -m cip_decision.demo
```

Four patients through the full pipeline, a care pathway, the approval lifecycle, CDS Hooks
discovery and cards, a workflow simulation, the labelled evaluation suite, and benchmarks. See
the [Phase 5 engineering report](../../docs/design/phase-5-engineering-report.md).

## What is deliberately not real yet

**The knowledge corpus is not clinical knowledge.** Textbook-level content with citations,
shaped like the real thing, reviewed by nobody.

**No licensed drug database.** A hand-maintained interaction list does not scale; the
maintenance burden is why those databases are commercial products.

**No measured override rate.** The suppression defaults come from published literature, not
from this system's behaviour in a real service. That is the metric that matters and it has
never been measured here.

**Nothing has run against a real EHR.** The CDS Hooks cards conform to the specification and
have never been rendered by a real CDS client. The SMART-on-FHIR module implements launch
context handling without requiring a live FHIR server, which is exactly as far as it goes.
