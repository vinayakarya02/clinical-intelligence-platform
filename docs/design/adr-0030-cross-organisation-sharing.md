# ADR-0030: Resolved identity never implies cross-organisation disclosure

**Status:** Accepted (Phase 6)

## Context

The EMPI's whole purpose is to determine that a record at Hospital A and a record at Lab B
describe one person. Having established that, the obvious next step is to present one unified
chart.

That obvious next step is a data breach.

Hospital A's records belong to Hospital A. The patient's consent to be treated at A is not
consent for B to read A's notes, and A's business associate agreements do not extend to B
because a matching algorithm scored 14.2. Identity resolution is a *statement about people*.
Disclosure is a *decision about organisations*, and conflating them means the EMPI silently
becomes an authorisation system.

## Decision

Identity resolution and disclosure are separate subsystems with no implicit connection.

A cross-organisation disclosure requires **all** of:

1. A resolved person link (the EMPI says these are the same human)
2. An explicit, dated, purpose-scoped **sharing agreement** between the two organisations
3. A **consent** that permits this purpose for this actor ([ADR-0028](adr-0028-consent-deny-by-default.md))
4. The requester's own ABAC attributes satisfying the target organisation's policy

Missing any one is a refusal, and the refusal names which one — an operator needs to know
whether to chase an agreement, a consent, or a role assignment.

Agreements are dated in both directions. An expired agreement stops working on its expiry date
without anyone remembering to disable it, which is the opposite of the usual failure where a
partnership ends and the integration keeps running for years.

The default direction of a query is *inward*: an organisation asks for data about a person, and
the holding organisation's policy decides. There is no global view that any single principal can
read.

## Consequences

- A unified longitudinal record is assembled **per requester**, and two requesters legitimately
  see different records for the same person. That is correct, and any test asserting a single
  canonical chart is asserting a bug.
- Population analytics run within an organisation's own boundary by default; cross-organisation
  cohorts require an agreement covering research or population health as a purpose, and
  de-identification is a separate control on top rather than a substitute for one.
- The tenant-in-constructor pattern from Phase 2 extends to organisation-in-constructor for
  every cross-organisation query object, so an unscoped query is a type error rather than a
  review finding.
