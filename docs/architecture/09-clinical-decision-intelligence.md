# Clinical Decision Intelligence — Architecture

**Status:** Phase 5 — implemented in [services/decision](../../services/decision/README.md)
**Depends on:** Phases 1–4, consumed unchanged
**Decisions:** [ADR-0019](../design/adr-0019-knowledge-as-data.md) … [ADR-0024](../design/adr-0024-human-approval-gate.md)
**Safety:** [clinical-safety-case.md](../safety/clinical-safety-case.md) — read this before the rest

## 0. The premise, and the finding that shapes it

Phases 1–4 built a system that answers questions. This phase builds one that *proposes actions*.
That is a categorical change in risk: an answer a clinician disagrees with is ignored, while a
recommendation a clinician disagrees with has already consumed their attention and, if it is
one of many, has trained them to stop reading.

The research finding that drove every design decision here: **CDS alert override rates in
production run between 49% and 96%, and roughly 300 reminders are required to prevent one
adverse drug event.** Alert fatigue is not a UX inconvenience — it is the dominant failure mode
of clinical decision support, and a system that fires on everything is *worse than no system*
because it degrades the clinician's response to the alerts that matter.

So the architecture optimises for **precision over recall in what reaches a human**, and makes
suppression a first-class, measured, auditable feature rather than an afterthought
([ADR-0021](../design/adr-0021-alert-fatigue.md)).

## 1. What is deterministic, and why

No language model participates in a decision. The reasoning is:

- A recommendation is a **regulated clinical claim**. FDA's CDS guidance turns on whether the
  clinician can understand its basis, and "the model concluded it" is not a basis.
- A rule that fires differently on identical input cannot be validated, and a knowledge base
  that cannot be validated cannot be clinically reviewed.
- The failure mode of a probabilistic recommender is a *plausible* recommendation, which is
  exactly the kind a busy clinician accepts.

The language model's role is unchanged from Phase 3: it composes prose from things that were
already decided. It never decides.

## 2. Knowledge is data, never code

Every clinical fact in this phase — rules, guidelines, interactions, dose limits, risk
models — is a **versioned, cited, dated artifact loaded from configuration**
([ADR-0019](../design/adr-0019-knowledge-as-data.md)). Nothing clinical is expressed in Python.

Three properties follow, and all three are requirements rather than conveniences:

- **A clinical reviewer can read the knowledge base** without reading code, which is the only
  way it can actually be reviewed.
- **An organisation can replace it.** NICE and WHO disagree; a US health system and an NHS
  trust have different formularies. A platform whose clinical content is compiled in cannot
  serve both.
- **Every recommendation traces to a citation and a version.** "Why?" is answerable with a
  document reference, not an appeal to the implementation.

> **The knowledge corpus shipped in this repository is a demonstration set.** It is small,
> it is drawn from well-established textbook pharmacology, and **it has not been clinically
> reviewed**. It exists to exercise the engine. See
> [clinical-safety-case.md](../safety/clinical-safety-case.md).

## 3. The decision pipeline

```
  trigger (hook, event, or question)
        │
        ▼
  ┌──────────────────────────────────────────────────────────┐
  │ 1. assemble   patient facts: timeline, labs, meds,        │
  │               conditions, allergies, demographics         │
  ├──────────────────────────────────────────────────────────┤
  │ 2. evaluate   rules whose conditions are satisfiable      │
  │               → fired rules + a full evaluation trace     │
  ├──────────────────────────────────────────────────────────┤
  │ 3. check      drug intelligence: interactions, duplicates,│
  │               contraindications, dose, allergy, organ     │
  ├──────────────────────────────────────────────────────────┤
  │ 4. score      configured risk models                      │
  ├──────────────────────────────────────────────────────────┤
  │ 5. rank       evidence quality × severity × recency       │
  ├──────────────────────────────────────────────────────────┤
  │ 6. detect     contradictions, missing information         │
  ├──────────────────────────────────────────────────────────┤
  │ 7. suppress   severity floor, role tailoring, dedup,      │
  │               and recently-overridden suppression         │
  ├──────────────────────────────────────────────────────────┤
  │ 8. explain    provenance chain per recommendation         │
  ├──────────────────────────────────────────────────────────┤
  │ 9. gate       human approval; nothing bypasses review     │
  └──────────────────────────────────────────────────────────┘
```

Stages 6 and 7 are the ones that distinguish this from a rules engine with a UI. Stage 6 is
what stops the system asserting two incompatible things; stage 7 is what stops it asserting
too many true things.

## 4. Severity and evidence quality are independent axes

Every clinical reference that survives in practice separates them, and this one does too
([ADR-0020](../design/adr-0020-severity-and-evidence.md)):

| Severity | Meaning |
|---|---|
| `contraindicated` | Should not be co-administered |
| `major` | May be life-threatening or cause permanent harm |
| `moderate` | May worsen the patient's condition or require additional care |
| `minor` | Bothersome, not medically detrimental |

| Evidence quality | Meaning |
|---|---|
| `established` | Well-documented in controlled study or label |
| `probable` | Very likely, documentation strong but not definitive |
| `suspected` | Plausible mechanism with limited documentation |
| `possible` | Documentation poor or conflicting |
| `theoretical` | Mechanism only, no clinical reports |

Collapsing these into one "importance" number is how a system ends up either suppressing a
contraindication with thin documentation, or shouting about a theoretical minor interaction.
**Severity decides whether it can be suppressed; evidence quality decides how it is worded.**

## 5. Care pathways follow FHIR

Pathways are modelled on **FHIR `PlanDefinition`**: a hierarchy of actions, each with an
*applicability condition* evaluated against patient facts, each pointing at an activity
definition. Applying a pathway to a patient produces a concrete plan — the FHIR
`$apply` semantics — rather than a static document.

Using the FHIR model rather than inventing a workflow language is the whole point: an
organisation already authoring `PlanDefinition` resources can bring them, and what this engine
produces is recognisable to a system that has never heard of it
([ADR-0023](../design/adr-0023-fhir-clinical-reasoning.md)).

## 6. CDS Hooks

The integration surface. Discovery, service invocation, and **Cards** as the specification
defines them — `summary` under 140 characters, `indicator` of `info`/`warning`/`critical`,
`source` with a label and URL, `suggestions` with typed actions, `overrideReasons`, `links`.

`medication-prescribe` is implemented **and marked deprecated**, because the specification
deprecated it in favour of `order-select` and `order-sign`. Shipping it without saying so
would encode a stale integration into every deployment that copied the example.

## 7. Nothing bypasses human review

Every recommendation enters a lifecycle: `proposed → under_review → (accepted | rejected |
expired)`. The transition is recorded with who, when, and — on rejection — why
([ADR-0024](../design/adr-0024-human-approval-gate.md)).

The rejection reason is the most valuable datum the system collects. It is the only direct
measurement of whether the knowledge base is right, and it feeds the suppression layer: a
recommendation a clinician has rejected for this patient does not fire again unchanged.

## 8. Evidence graph

Extends the Phase 2 knowledge graph rather than replacing it. A recommendation's provenance is
a path:

```
Guideline ──cites──► Evidence ──supports──► Rule ──fired_on──► Fact ──produced──► Recommendation
```

The graph is what makes "how was this produced" a query rather than an archaeology exercise,
and it is stored so the answer survives a knowledge-base upgrade that changed the rule.

## 9. How it is measured

Four numbers, reported side by side and never combined
([harness](../../services/decision/src/cip_decision/evaluation/harness.py)):

| Metric | What it catches |
|---|---|
| Rule recall against labelled cases | A rule that stopped firing |
| False positives against **forbidden** labels | A rule that fires when it must not — where all three Phase 5 Blockers lived |
| Alert burden: mean and maximum alerts per case | A change that buys recall by alerting more |
| Rule coverage | Corpus knowledge no case has ever seen execute |

Combining them into a single score would let an engine that alerts on everything look like an
improvement, which is precisely the failure the alert-fatigue literature describes. So they are
kept apart, and a run is only clean if it is also deterministic, suppressed no contraindication,
and triggered no forbidden rule — regardless of how good the accuracy figures are.

Coverage earns its place: on its first run it reported a corpus rule that no case exercised.
That rule was correct; nobody had ever watched it execute.

**The number that matters most cannot be measured here.** The real-world override rate needs
clinicians using the system on their own patients. These burden figures are an upper bound on
what would reach a screen, not evidence that the screen is tolerable.

## 10. What this deliberately is not

**Not autonomous.** It proposes; a clinician disposes. There is no path from a rule firing to
an order being placed.

**Not a validated knowledge base.** The engine is production-grade. The clinical content is a
demonstration corpus that requires review by a qualified clinician before any use with real
patients, and the safety case says so at length.

**Not a diagnosis engine.** It evaluates explicit rules against recorded facts. It does not
infer conditions that are not documented.
