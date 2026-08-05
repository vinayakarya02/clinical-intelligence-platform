# ADR-0034: A metric is a declaration, not a query

**Status:** Accepted (Phase 7)

## Context

The characteristic failure of a BI layer is not a wrong query. It is **two correct queries that
disagree**: the clinical dashboard says the readmission rate is 14.2%, the executive deck says
11.8%, and both are right about different denominators. Nobody can tell which is wrong because
neither is, and trust in the whole layer goes with it.

This happens because a metric implemented as a query lives wherever it was written. The second
person who needs it writes it again, slightly differently — a different date attribution, a
different exclusion, a different treatment of retracted records.

## Decision

Metrics are **declared once, in data**, and compiled: a name, a grain, a source fact, an
aggregation, filters, an optional denominator, and a disclosure policy. Dashboards and reports
reference a metric **by key**; they cannot express an aggregation of their own.

The same house pattern as clinical rules ([ADR-0019](adr-0019-knowledge-as-data.md)) and
interface mappings ([ADR-0026](adr-0026-mapping-as-data.md)), for the same reason: the person who
knows whether a denominator is right is an analyst, not a Python developer, and a definition in
code is invisible to them.

The registry refuses at load:

- an unknown source fact or column — a metric over a table that does not exist fails loudly
  rather than returning zero, and **zero is the most dangerous wrong answer** because it looks
  like a real finding
- a ratio metric with no denominator, or a denominator that could be zero without a declared
  behaviour
- a metric with no declared disclosure policy — a metric nobody classified is one that gets
  published at whatever the default is
- two metrics sharing a key
- a filter referencing a column that is not on the fact's grain

**Version and effective date on every metric.** A definition that changes silently makes a
historical chart un-reproducible: the same query over the same data returns different numbers on
different days, and nobody can say why.

## Consequences

- One number per question, everywhere. A dashboard and a scheduled report showing the same
  metric key are showing the same computation by construction.
- Adding a metric is a reviewed data change with a version bump, not a code deployment. That is
  slower than writing a query, and it is the point.
- Analysts cannot express a genuinely novel aggregation without a new declaration. This is real
  friction; the alternative is a free-form query surface, which
  [ADR-0035](adr-0035-no-free-form-queries.md) refuses on separate grounds.
- Every result carries the metric key and version that produced it, so a chart can be
  reconstructed later.
