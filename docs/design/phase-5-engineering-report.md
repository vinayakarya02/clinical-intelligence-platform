# Phase 5 Engineering Report — Clinical Decision Intelligence

> **The clinical knowledge corpus in this repository has not been reviewed by a clinician and
> must not be used in the care of real patients.** It is a demonstration set that exists to
> exercise the engine. Read [clinical-safety-case.md](../safety/clinical-safety-case.md) first.

**Scope delivered:** a deterministic clinical decision engine, a rules engine over a safe typed
expression language, a versioned cited knowledge base, drug intelligence across seven checks,
configurable risk stratification, FHIR-shaped care pathways, CDS Hooks, SMART-on-FHIR
interfaces, event-driven clinical workflows, an evidence graph, a human approval gate, alert
suppression, and an evaluation framework measuring accuracy against alert burden.

**Verification status:** 932 of 943 tests pass, 11 skip (integration tests needing live
PostgreSQL/MongoDB/Neo4j and OCR needing Tesseract). `ruff format`, `ruff check`, and `pyright`
clean. The labelled evaluation suite passes 5/5 at 100% rule coverage. Phases 1–4 consumed
unchanged.

---

## 1. Architecture summary

Pipeline: **assemble → evaluate → check → score → rank → detect → suppress → explain → gate**
([09-clinical-decision-intelligence.md](../architecture/09-clinical-decision-intelligence.md)).

Three properties define it:

**Deterministic.** No model participates in a decision
([ADR-0022](adr-0022-deterministic-decisions.md)). The same facts and the same knowledge base
produce the same recommendations, in the same order, every time — which is what makes a
knowledge base clinically reviewable, because a reviewer can reason about what it will do.

**Knowledge is data.** Rules, guidelines, interactions, dose limits, risk models, and pathways
are versioned, cited, dated YAML ([ADR-0019](adr-0019-knowledge-as-data.md)). The engine
contains no clinical content. A test enforces it: engine code mentioning a drug name fails CI.

**Nothing acts.** Every recommendation enters a lifecycle that cannot reach `accepted` without
an identified human ([ADR-0024](adr-0024-human-approval-gate.md)). There is no auto-accept and
no flag that disables the gate.

## 2. Research findings applied

| Finding | Applied as |
|---|---|
| CDS override rates run **49–96%**; ~300 reminders prevent one adverse drug event; **role tailoring** is the most effective mitigation | Suppression is a designed, measured, audited pipeline stage with a per-role severity floor ([ADR-0021](adr-0021-alert-fatigue.md)) |
| Every surviving drug reference separates **severity** from **documentation quality** | Two independent enumerations that are never multiplied ([ADR-0020](adr-0020-severity-and-evidence.md)) |
| FHIR `PlanDefinition`: actions with applicability conditions, applied via `$apply` | Pathways are that shape, evaluated by the same rules engine ([ADR-0023](adr-0023-fhir-clinical-reasoning.md)) |
| CDS Hooks 2.0 card schema — summary under 140 chars, three indicators, structured override reasons | Implemented to the specification; the 140-char limit is a constructor invariant |
| CDS Hooks **deprecated `medication-prescribe`** in favour of `order-select`/`order-sign` | Implemented *and marked deprecated in discovery*, so an integrator sees it before building |
| FDA CDS guidance turns on whether the clinician can understand the basis | Citation and provenance are constructor invariants on `Recommendation` |

## 3. Decision engine design

A `Recommendation` **cannot be constructed** without a citation and a provenance chain. That is
the structural form of "recommendations must always explain WHY" — a type constraint, not a
review checklist item.

The condition language is a typed AST with **no `eval`**. A knowledge base is an
operator-editable file; evaluating it as Python is remote code execution with a clinical
veneer. The language is deliberately small — presence, comparison, temporal windows, trend,
boolean composition — so that anything it cannot express requires a *new operator, reviewed
once*, rather than an escape hatch.

The most consequential design decision is that **evaluation has three outcomes, not two**. A
rule about potassium on a patient with no potassium recorded is `unknown`, not false. Unknown
never fires, never negates to true, and feeds the missing-information detector. Treating it as
false would silently convert "we do not know" into "no concern" — which is how a CDS reassures
a clinician about a patient it never assessed.

## 4. Workflow engine design

Clinical events (`lab_result_available`, `medication_prescribed`, …) trigger a decision run and
a notification. Built on the Phase 4 event spine, so a clinical workflow inherits correlation,
causation, tracing, and automatic audit.

**Notification has its own severity floor, separate from suppression.** A recommendation can be
worth showing on a screen the clinician is already looking at and not worth interrupting them
for; collapsing the two either pages constantly or buries the urgent case.

The `Notifier` protocol has no default implementation. A workflow that logs loudly when nobody
is listening is better than one that invents a delivery channel — and a deployment that forgot
to wire notification should discover it in configuration, not by a clinician never being told.

## 5. Care pathway design

FHIR `PlanDefinition` semantics: a tree of actions, each with an applicability condition
evaluated by the *same* rules engine, applied to a patient to produce a concrete plan.

Two decisions matter. **Applicability conditions reuse the rules engine**, so a pathway
condition and a standalone rule get the same evaluator, trace, and explanation — one thing to
review rather than two. And **not-applicable actions are retained with their reason**, because
dropping them makes the produced plan indistinguishable from one where the action was never
considered, and "we checked and it does not apply" is clinically different from silence.

## 6. Guideline engine design

Guidelines, rules, pathways, and risk models all carry `version`, `effective_from`, and
`effective_until`. Dates rather than an enabled flag: a guideline supersession *has* a date, and
a knowledge base that can only say "enabled" cannot represent "this became the standard in
March". The shipped corpus includes a superseded rule version to exercise it — 6 of 7 rules are
active today.

The loader refuses: an artifact with no citation, an unknown key (a misspelled `severity` would
silently default, turning a contraindication into an informational note), a duplicate
`rule_id@version` (which one is active would depend on file order), and an unsupported condition
operator (a rule that silently never fires is indistinguishable from one that works).

## 7. Bugs found

All found by the end-to-end run or the adversarial pass. None by unit tests written alongside
the code.

### Blockers

**B1 — A stroke-risk score was computed for patients without atrial fibrillation.**
CHA2DS2-VASc estimates stroke risk *in non-valvular AF*. The engine scored and **banded** it for
every patient — the thin-record patient was labelled "intermediate risk". A clinically
meaningless number that looks authoritative, and one that could drive inappropriate
anticoagulation. This is precisely the "fake medical logic" the phase forbids.

**B2 — An allergy to one statin contraindicated every statin.** Class-based allergy matching was
applied to *all* classes. Correct for beta-lactams, wrong for statins, and the failure direction
denies a patient a needed drug on no evidence.

**B3 — A genuine contraindication was silently suppressed.** The deduplication concern was keyed
on the knowledge-entry id. A class-level interaction entry matches several distinct drug pairs,
so "clarithromycin + atorvastatin" was folded into "simvastatin + clarithromycin" and
disappeared — two different contraindications, one alert.

### High

**H4 — The contradiction detector was a false-positive generator.** It flagged five conflicts on
one patient, all spurious: "avoid this drug (allergy)" versus "avoid this drug (interaction)"
were reported as a disagreement when they are two reasons for one action. A second attempt
inferring direction from wording flagged "2 statin agents **prescribed** concurrently" as
recommending *toward* a statin. False conflicts cost exactly the attention alert-fatigue
research identifies as scarce.

**H5 — The evidence graph asserted that one medication led to another.** Provenance subjects
were chained, so `drug_check → Lisinopril → Spironolactone → recommendation` claimed a
derivation between two parallel inputs.

**H6 — An incomplete risk score's upper bound always equalled its lower bound.** The calculation
matched component *labels* against component *ids*, so it never matched. The "lower bound"
warning was present and the number that tells a clinician how much risk might be hidden was
silently wrong.

**H7 — Unbounded clinical state.** 200 patients produced 600 approval records and 3,000 graph
edges, growing forever — a memory leak retaining clinical recommendations tied to patient ids
for the life of the process.

**H8 — `decide(policy=…)` was accepted and ignored.** One engine serving a prescriber and a
pharmacist silently applied the wrong severity floor to one of them.

### Medium

**M9 — `knowledge/factory.py` imported sideways.** Found by the boundary test on its first run.
**M10 — An unknown condition operator produced a generic message** and made the precise error
below it unreachable. **M11 — structlog reserves `event`**, which crashed the workflow
simulation.

## 8. Bugs fixed

All eleven, each with a regression test:

- Risk models declare `applies_when`; an inapplicable model reports **no score at all** rather
  than zero, because zero is a value a clinician can act on. The demo display honours it too.
- Allergy cross-reactivity must be **declared** per class; the default is exact-ingredient.
- The dedup concern includes the actual subjects, so distinct pairs stay distinct.
- Contradiction direction is **declared by the knowledge author, never inferred from prose**.
  Two attempts at inference produced false conflicts; an undeclared direction now participates
  in no contradiction. Missing a contradiction is recoverable by the reviewing clinician the
  approval gate guarantees; fabricating one is not.
- Provenance subjects attach directly to the recommendation rather than chaining.
- Unevaluable points are carried explicitly rather than re-derived.
- Approval records, the evidence graph, and override memory are bounded — **with open approval
  records never evicted**, because dropping a pending review loses a clinical decision somebody
  is waiting on.
- A per-call policy is honoured; the factory moved above what it builds; the operator error is
  precise; the log field renamed.

## 9. Benchmarks

In-process, deterministic, no network. **No model call in the decision path** (ADR-0022), so
these are real latencies rather than a lower bound.

| Operation | Rate | Per operation |
|---|---|---|
| risk scoring (2 models) | 1,341 /s | 0.75 ms |
| pathway application | 1,958 /s | 0.51 ms |
| rule evaluation (7 rules) | 576 /s | 1.74 ms |
| drug checks (5 meds, 10 pairs) | 483 /s | 2.07 ms |
| **full decision pipeline** | **138 /s** | **7.24 ms** |
| decision: complex patient | 156 /s | 6.42 ms |
| decision: thin record | 274 /s | 3.65 ms |
| workflow event (decide + notify) | 76 /s | 13.14 ms |

Peak memory 2.63 MB. A CDS Hooks call is one decision per patient view, so 7 ms is comfortably
inside an interactive budget. The workflow path is slower because it runs a full decision plus
notification per event. The evaluation suite measures the same pipeline at p50 3.6 ms and
p95 3.8 ms across five patients of varying density.

## 10. Test summary

**943 tests: 932 pass, 11 skip.** 99 are new in Phase 5.

The load-bearing ones are the three boundary tests — including one asserting that **no drug name
appears in engine code**, which is how ADR-0019 stays true rather than aspirational — and the
regressions for B1, B2, B3, H4, H5, H6, H7, and H8.

### The labelled evaluation suite

Five labelled patients, scored on four metrics that are reported side by side and never
combined into one number:

```
  cases                        5/5 passed
  rule recall                  100.0%
  rule precision               100.0%
  false positives              0
  alerts per case              1.60 mean, 3 max
  suppressed by policy         46.7%
  cases with no alert          20.0%
  rule coverage                100.0%
  explanation completeness     100.0%
  contraindications suppressed 0
  deterministic                yes
  latency                      p50 2.83 ms, p95 4.06 ms
```

The **forbidden** labels are the interesting half. Each is a specific wrong answer this system
produced during Phase 5 — a stroke score for a patient without the arrhythmia, an allergy
generalised across a drug class — encoded so it fails loudly rather than averaging away into a
precision figure.

The suite's first run was worth more than its current pass rate. It failed 4 of 5 cases and
produced three findings: two of my labels were wrong (an eGFR threshold I had misremembered and
a risk model I wrongly assumed had a population precondition), and **rule coverage named a
corpus rule that no case exercised** — knowledge that was correct and that nobody had ever
watched execute. A case now covers it, which is also the suite's only `contraindicated`
severity and therefore the case that catches a broken suppression exemption.

The suite also confirms the honest limit: 46.7% of what the engine produced was suppressed
before display. That is a plausible-looking number derived from published literature and this
corpus, not evidence about how a clinician would experience it.

## 11. Production readiness

**Ready:** the engine. Determinism, the safe expression language, the three-valued evaluation,
the citation and provenance invariants, versioning with activation dates, the suppression layer,
the approval gate, and the CDS Hooks conformance are all production-grade and tested.

**Not ready, in order of severity:**

1. **The knowledge corpus has not been clinically reviewed.** It is textbook-level content with
   citations, and it is test data shaped like clinical content. It is *convincing*, which is
   what makes it dangerous.
2. **No validation against a reference drug database.** A hand-maintained interaction list is
   not a viable clinical artifact; the maintenance burden is why those databases are commercial.
3. **No measurement of alert burden with real clinicians.** The suppression defaults come from
   published literature, not from this system's behaviour in a real service. Override rate is
   the metric that matters and it has never been measured here.
4. **Nothing has run against real infrastructure or a real EHR.** Inherited from Phases 2–4 and
   still true. The CDS Hooks cards have never been rendered by a real CDS client.
5. **Known clinical limitations** — pairwise interactions only, no dose calculation, no
   adherence reasoning, no pregnancy/paediatric/geriatric logic. Each is in the safety case.
6. **Possibly a regulated medical device.** Depending on jurisdiction and claims, deploying this
   requires a regulatory assessment that has not been done.

**Assessment:** Phase 5 delivers a clinical decision *engine* built to production standards
around a knowledge base that is explicitly not clinical knowledge. The separation is the point:
the engine can be validated by software engineering, and the content can only be validated by
clinicians — so the architecture makes the content reviewable, replaceable, and traceable rather
than pretending the software can substitute for that review.

The honest summary of the phase is that **the end-to-end run found three Blockers that 84 unit
tests did not**, and two of the three were cases where the system produced a confident,
plausible, clinically wrong output: a stroke score for a patient without the arrhythmia, and a
contraindication for a drug the patient was not allergic to. Both would have looked correct to
anyone not checking the specific claim. That is the failure mode of clinical decision support,
and it is the argument for the approval gate that ADR-0024 refuses to make optional.

## 12. Technical debt

| Item | Why it matters | Next |
|---|---|---|
| Corpus unreviewed | The largest gap in the phase | Before any use |
| No licensed drug database | Hand-maintained interactions do not scale | Before any use |
| Pairwise interactions only | Three-drug interactions exist | With a real database |
| No CQL support | FHIR's expression language; ours is a subset | If integrating published PlanDefinitions |
| Suppression defaults unvalidated | Drawn from literature, not from this service | With real clinicians |
| Labelled suite is five cases | Enough for coverage, not for a distribution | As the corpus grows |
| No `RequestGroup` cardinality behaviours | FHIR pathway features not implemented | With EHR integration |
| Approval records in-process | Production needs a database; open records are unbounded until `expire_stale` runs | With persistence |
| Decision engine not wired to the gateway | No HTTP surface serves CDS Hooks | With the gateway routes |
