# Clinical Safety Case

**Read this before deploying, demonstrating, or evaluating the decision engine.**

## 1. The single most important statement

**The clinical knowledge corpus in this repository has not been reviewed by a clinician and
must not be used in the care of real patients.**

It is a demonstration set — small, drawn from well-established textbook pharmacology, and
present to exercise the engine. It is *convincing*, which is exactly what makes it dangerous:
a knowledge base that looks authoritative invites deployment. The engine is built to production
standards; the content is not clinical content, it is test data shaped like clinical content.

## 2. What has been engineered, and what has not

| Engineered to production standard | Not done |
|---|---|
| Rules engine, evaluation, tracing | Clinical review of any rule |
| Knowledge loading, versioning, activation dates | Curation of a real knowledge base |
| Severity / evidence separation | Validation against a reference drug database |
| Suppression, deduplication, override memory | Measurement of override rates with real clinicians |
| Approval lifecycle and audit | Integration with a real EHR |
| CDS Hooks card construction | Conformance testing against a real CDS client |
| Care pathway application | Clinical validation of any pathway |
| Risk score framework | Validation of any risk model against outcomes |

## 3. Safety assumptions

Each of these is an assumption the design *depends on*. If one is false in a deployment, the
corresponding protection is absent.

1. **A qualified clinician reviews the knowledge base before use.** Nothing in the software
   checks this, and nothing can.
2. **A clinician reviews every recommendation.** Enforced in code — there is no auto-accept
   path ([ADR-0024](../design/adr-0024-human-approval-gate.md)).
3. **The patient data given to the engine is accurate and current.** The engine reasons over
   what it is given; a stale medication list produces confident wrong output, and the
   missing-information detector catches absence, not staleness in a record that claims to be
   current.
4. **Suppression is configured deliberately.** The defaults are drawn from the alert-fatigue
   literature, not from any particular institution's risk appetite
   ([ADR-0021](../design/adr-0021-alert-fatigue.md)).
5. **Absence of an alert means the knowledge base had no rule.** It does not mean the
   combination is safe. This is the most consequential misreading available, and it is why
   every "no findings" result states it explicitly.

## 4. Known limitations

- **No temporal reasoning about medication adherence.** The engine knows what is prescribed,
  not what is taken.
- **No dose calculation.** Dose *limits* are checked against configured ranges; the engine does
  not compute a dose, which is a decision requiring weight, renal function, indication, and
  clinical judgement.
- **Renal and hepatic adjustment is a flag, not a recommendation.** It says an adjustment may
  be needed and cites why; it does not say what the adjusted dose is.
- **Risk scores are the published formulas only.** No model-derived risk, because a
  model-derived risk estimate about a patient is a regulated claim rather than a calculation.
- **No pregnancy, paediatric, or geriatric-specific logic.** Their absence is a limitation, not
  an oversight — each is a specialty knowledge domain.
- **Interaction checking is pairwise.** Three-drug interactions are not modelled.

## 5. Failure modes and what bounds them

| Failure | Bound |
|---|---|
| Wrong rule fires | Human approval gate; rejection reason feeds back |
| Rule fails to fire | Coverage measurement; explicit "no rule existed" in output |
| Too many alerts | Suppression layer, measured suppression and override rates |
| Stale knowledge | Activation and expiry dates; expired artifacts do not evaluate |
| Contradictory recommendations | Contradiction detector; conflicting sets are surfaced together, never silently resolved |
| Missing patient data | Missing-information detector names what was needed |
| Knowledge base tampering | Artifacts are validated on load; citations are mandatory |

## 6. What would be required before real use

1. Clinical review and sign-off of every knowledge artifact by a qualified clinician.
2. Validation of drug content against a maintained reference database under licence.
3. Prospective evaluation of alert burden and override rates with real clinicians.
4. Integration testing against a real EHR and a real FHIR server.
5. A regulatory assessment. Depending on jurisdiction and claims, this may be a regulated
   medical device.
6. A clinical governance process for knowledge updates, with versioning and rollback — which
   the engine supports and which needs an owning committee, not a feature.
