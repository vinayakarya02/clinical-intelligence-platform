# ADR-0032: One canonical model, four projections, and a CapabilityStatement that understates

**Status:** Accepted (Phase 6)

## Context

This phase adds four ways to get data out — REST, FHIR, asynchronous events, and bulk export —
on top of the three Phase 3 output renderings. Each has its own conventions, and the naive
implementation gives each its own path from storage, which produces four subtly different
answers to the same question.

It also raises a versioning question with two independent axes: the *API* version (this
platform's contract) and the *FHIR* version (R4 or R5), which change on different schedules for
different reasons.

## Decision

**One canonical internal model; every surface is a projection of it.** Consent evaluation,
tenant scoping, and ABAC happen in the canonical layer, *below* the projections. A new output
format cannot accidentally omit an authorisation check, because the format layer never touches
storage.

**API version in the path (`/v1/…`); FHIR version negotiated by content type**
(`application/fhir+json; fhirVersion=4.0`). They are separate axes: supporting R5 must not
require a `/v2`, and a breaking change to this platform's own contract must not be disguised as
a FHIR upgrade. Both R4 and R5 are served from the same canonical model, with the R4/R5
differences that affect the implemented resources handled in the projection.

**Resource versioning is optimistic with weak ETags.** `If-Match` on update; a mismatch is
`412`, never a silent overwrite. FHIR deviates from RFC 7232 in using weak ETags, and this
implementation follows FHIR rather than the RFC, because the counterparty is a FHIR client.

**Bulk export is asynchronous with a manifest**: kick-off returns `202` and a status location,
polling returns progress, completion returns a manifest of NDJSON files. Export runs against a
snapshot, so a manifest cannot describe files whose contents changed while it was being written.

**The `CapabilityStatement` is generated from what is actually registered**, not hand-written.
A hand-written one drifts, and a client that trusts a capability the server does not have fails
at the worst time. Search parameters that are not implemented are absent from it — the
statement understates rather than overstates.

## Consequences

- Adding a surface means adding a projection, which is mechanical. Adding a *capability* means
  changing the canonical layer, which is where review effort belongs.
- Serving two FHIR versions doubles the projection test matrix for affected resources. Accepted:
  the field runs both, and a platform that supports only one is unusable at half its
  counterparties.
- Search is deliberately partial. The full FHIR search grammar (chaining, `_include`,
  `_has`, composite parameters) is a large surface, and an incomplete implementation that
  *claims* completeness silently returns wrong result sets. Declaring the subset is the safe
  failure.
- Bulk export snapshots cost storage and are bounded by retention, which is stated in the
  manifest rather than left for a client to discover when a URL expires.
