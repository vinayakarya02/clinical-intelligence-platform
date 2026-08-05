# ADR-0033: The warehouse is de-identified at load, not at query

**Status:** Accepted (Phase 7)

## Context

The analytics warehouse answers population questions — cohort sizes, adverse-event trends,
ingestion throughput, access summaries. Almost none of those need to know *which* patient.

There are two places de-identification can happen:

- **At query time.** The warehouse holds identified data and the query layer strips or
  aggregates before returning. Flexible; supports re-identified analytics with one code path.
- **At load time.** The ETL de-identifies on the way in, and the warehouse simply does not
  contain the identifiers.

Query-time is the common choice because it is easier and keeps one copy of the data.

## Decision

De-identify at load. The analytics warehouse contains no direct identifiers, and cannot, because
nothing ever writes them there.

The reasoning is a difference in failure mode, not in effort:

- Query-time de-identification means **every query path is a potential disclosure**, and a new
  endpoint, a new export format, or a debug dump added under time pressure is a new opportunity
  to leak. The control is only as good as the discipline of everyone who ever adds a read path.
- Load-time de-identification means a bug in the query layer returns **de-identified data
  wrongly aggregated**, which is an accuracy incident rather than a privacy incident.

Patient-level, re-identified analytics remain possible and are a **separate store, a separate
elevated scope, and an audited PHI access event** — not a flag on the same query. A flag on the
same query is how the safe path and the dangerous path end up one boolean apart.

The ETL reuses Phase 6's Safe Harbor implementation
([ADR-0031](adr-0031-deidentification-safe-harbor.md)) rather than reimplementing it, so there is
one de-identification ruleset in the platform and one place to review it.

## Consequences

- **Some analyses become impossible on the warehouse**, and that is the trade being made
  deliberately. Day-resolution time-to-event needs exact dates; Safe Harbor removes them. Those
  analyses go to the elevated path with a named requester and an audit record.
- The warehouse can be given a broader read audience than the operational store, which is the
  point: analysts get self-service without every query being a minimum-necessary decision.
- **The pseudonym salt never enters the warehouse.** Storing it beside the pseudonyms would make
  the mapping reversible by anyone holding the warehouse, which is the whole thing being
  prevented.
- Reprocessing is required when the de-identification ruleset changes, because the warehouse
  holds the *output* of a ruleset version and that version is recorded in every load.
