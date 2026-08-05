# ADR-0035: No free-form query surface reaches the warehouse

**Status:** Accepted (Phase 7)

## Context

The Phase 0 design already committed to this
([05-analytics-dashboard.md §4](../architecture/05-analytics-dashboard.md#4-api-surface)):
parameterised, RBAC-scoped query templates only, no SQL or Cypher exposed. This ADR records why
that survives contact with the obvious counter-argument, which is that analysts want SQL and will
ask for it.

Two distinct risks, and only the first is the one people think of:

**Injection.** The familiar one, and the easy one — parameter binding solves it.

**Inadvertent disclosure through query construction.** The one that actually matters here. A
perfectly safe SQL dialect, correctly parameterised, still lets an analyst write
`GROUP BY zip3, birth_year, sex` and single out a patient without any injection at all. The
query is valid, the data is de-identified, and the result identifies someone. No amount of
escaping addresses it, because nothing is being escaped.

## Decision

The only query surface is a registry of **typed templates**. A template declares its parameters
with types, allowed values, and bounds; the executor refuses anything else. There is no path
from a caller's string to a query plan.

Specifically refused:

- a parameter not declared on the template
- a value outside a declared enumeration or numeric bound
- a `group_by` naming a dimension the template does not permit — which is what stops the
  quasi-identifier combination above
- a date range wider than the template's maximum, because an unbounded range is both a
  denial-of-service and, on a small population, a re-identification aid
- more grouping dimensions than the template permits, regardless of which ones

Templates carry the **minimum scope** required to run them, so authorisation is a property of
the template rather than a check somebody remembers to add at the call site.

## Consequences

- **Genuinely novel analysis requires a new template**, reviewed once. Analysts will find this
  restrictive, and the honest answer is that the restriction is the control — a self-service
  surface that can express any grouping cannot also promise non-disclosure.
- The template registry becomes a governance artifact: what can be asked of this data is
  enumerable and reviewable, which is a question a compliance officer can actually answer.
- No SQL parser, no dialect handling, no query rewriting. The executor works on typed plans, so
  there is no string to sanitise anywhere in it.
- An escape hatch for data scientists is a **separate, elevated, audited export path**
  ([ADR-0033](adr-0033-deidentify-at-load.md)), not a widening of this surface. Keeping them
  separate is what stops the safe path acquiring a `raw_sql` parameter under deadline pressure.
