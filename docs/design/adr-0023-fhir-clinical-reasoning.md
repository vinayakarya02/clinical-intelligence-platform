# ADR-0023: Care pathways follow FHIR PlanDefinition semantics

**Status:** Accepted (Phase 5)

## Context

Care pathways need a representation: diagnosis → investigations → treatment → monitoring →
follow-up → discharge, with branches that depend on the patient. The options are a bespoke
graph format, BPMN, or FHIR's Clinical Reasoning module.

FHIR already models exactly this. `PlanDefinition` is a hierarchy of actions; each action
carries an *applicability condition* and points at an `ActivityDefinition`; applying it to a
patient produces concrete request resources grouped in a `CarePlan` or `RequestGroup`. The
`$apply` operation is the semantics we need.

BPMN models business processes well and clinical *applicability* poorly — its gateways express
control flow, not "this action applies if eGFR is below 30".

## Decision

Pathways are `PlanDefinition`-shaped: a tree of actions, each with an applicability condition
evaluated by the same rules engine that evaluates everything else, each referencing an
activity. Applying a pathway to a patient's facts produces a concrete plan with each action
marked applicable, not-applicable, or blocked — **and the reason**, in every case.

Reusing the rules engine for applicability conditions is the detail that matters. A pathway
condition and a standalone rule are the same kind of thing, so they get the same evaluator,
the same trace, and the same explanation.

## Consequences

- An organisation already authoring `PlanDefinition` resources can bring them, and what this
  engine emits is recognisable to a system that has never heard of it.
- FHIR's full expressiveness is not implemented — no `RequestGroup` cardinality behaviours, no
  CQL. The subset is what a pathway actually needs, and the gap is documented rather than
  papered over.
- A not-applicable action is retained in the output with its reason. Dropping it would make the
  produced plan indistinguishable from one where the action was never considered, and "we
  checked and it does not apply" is clinically different from silence.
