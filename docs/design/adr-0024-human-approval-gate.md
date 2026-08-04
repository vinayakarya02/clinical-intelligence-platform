# ADR-0024: Nothing reaches a patient record without human review

**Status:** Accepted (Phase 5)

## Context

The system now produces recommendations. The question is what may happen to one automatically.

The efficiency argument for auto-acceptance is real: a low-risk, high-confidence, guideline-
backed recommendation that a clinician would approve every time costs attention to approve. In
a busy service that cost is not trivial.

## Decision

Every recommendation enters a lifecycle — `proposed → under_review → (accepted | rejected |
expired)` — and **no transition to `accepted` happens without an identified human**. There is
no auto-accept, no confidence threshold above which review is skipped, and no configuration
flag that disables the gate.

The reasoning is that the gate is not primarily about the *recommendation*; it is about
accountability. A clinical action needs an accountable clinician, and a system that can place
one without a human has moved the accountability to the vendor while leaving the consequence
with the patient.

A rejection **requires a reason**. That is the most valuable datum the system collects: the
only direct measurement of whether the knowledge base is right, and the input to the
suppression layer.

Expiry exists because a recommendation about a lab value from three days ago is not a pending
decision, it is stale. An expired recommendation is closed, recorded, and not silently
resurfaced.

## Consequences

- The system cannot act. That is the design, and it bounds the harm the knowledge base can do
  to "wasted attention" rather than "wrong order placed".
- Approval is a bottleneck by construction, which is why the suppression layer matters: the
  scarce resource being protected is clinician attention, and every alert spends it.
- The audit trail is a Phase 1 hash-chained record, so an approval cannot be backdated or
  removed without detection.
