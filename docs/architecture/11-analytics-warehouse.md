# Analytics Warehouse & Self-Service Reporting

Phase 7. Realises the Phase 0 design in
[05-analytics-dashboard.md](05-analytics-dashboard.md): a dimensional warehouse fed by a
de-identifying ETL, a declarative metric layer, a template-only query surface, four dashboard
categories, and scheduled reports.

---

## 0. What this is not, and why the distinction matters

Phase 6 already ships something called dashboards. They are a different thing and confusing them
would produce one system that does neither job well.

| | Phase 6 `cip_interop.dashboards` | Phase 7 `cip_analytics` |
|---|---|---|
| Question | "What is happening right now?" | "What happened, and how does it compare?" |
| Source | The live event stream | A warehouse loaded on a schedule |
| Window | Minutes, sliding | Years, dimensional |
| History | None — it forgets | The point of it |
| Data | Counts only, never PHI | De-identified facts and conformed dimensions |
| Consumer | Operators watching an interface | Analysts asking a question |

Real-time telemetry that tries to answer historical questions grows unbounded state; a warehouse
that tries to answer real-time questions is always stale. They stay separate, and Phase 7 does
not touch Phase 6's.

## 1. Shape

```
  operational store ─┐
  FHIR repository  ──┤
  event stream ──────┼─► ETL ──► de-identify ──► conform ──► load ──► ★ warehouse
  audit log ─────────┤     (watermarked,          (Safe        (star     (facts +
  copilot telemetry ─┘      incremental)          Harbor)      schema)    dimensions)
                                                                              │
                                                     ┌────────────────────────┘
                                                     ▼
                                          ┌─────────────────────┐
                                          │ semantic layer      │  metrics declared once
                                          │ (metric registry)   │  and compiled
                                          └─────────────────────┘
                                                     │
                                          ┌─────────────────────┐
                                          │ query templates     │  typed parameters only
                                          │ + RBAC scope        │  no free-form surface
                                          └─────────────────────┘
                                                     │
                                          ┌─────────────────────┐
                                          │ disclosure control  │  primary + complementary
                                          └─────────────────────┘
                                                     │
                              ┌──────────────────────┼──────────────────────┐
                              ▼                      ▼                      ▼
                        dashboards            scheduled reports       analytics API
                   (4 categories)          (definition + delivery)   (/analytics/*)
```

Every stage below the warehouse is read-only. Nothing downstream of the ETL can write a fact,
which is what makes the warehouse reproducible from its sources.

## 2. The star schema

Conformed dimensions shared by every fact, so two facts grouped by the same dimension are
grouped the same way. That is what "conformed" buys and it is the reason a dimensional model is
used rather than a pile of aggregate tables.

**Dimensions:** date, organisation, cohort, clinical code, actor role, source system,
document type.

**Facts:** encounter, observation, document ingestion, retrieval query, copilot answer, PHI
access, de-identification job.

Two grain rules, both of which are the usual source of double-counting:

- **A fact table has one grain and it is declared.** `fact_observation` is one row per
  observation, not per patient and not per encounter. Mixing grains in one table makes every
  `COUNT` ambiguous.
- **Additivity is declared per measure.** A count is additive across every dimension; a rate is
  not additive at all and must be recomputed from its numerator and denominator at the grouping
  level asked for. Summing a rate is the most common wrong number in a BI layer, so `RATIO`
  measures refuse to be summed.

## 3. De-identified at load

The warehouse holds no direct identifiers because nothing writes them there
([ADR-0033](../design/adr-0033-deidentify-at-load.md)). Patient keys are salted pseudonyms;
dates are reduced to a date dimension at day-of-year granularity or coarser depending on the
fact's policy; geography is three-digit postal at most.

Re-identified analysis is a **separate elevated path** with a named requester and an audited PHI
access event, not a parameter on this one.

## 4. Metrics are declared

A metric is a key, a version, a grain, a source fact, an aggregation, filters, an optional
denominator, and a disclosure policy — in data, loaded and validated at startup
([ADR-0034](../design/adr-0034-metric-is-a-definition.md)). Dashboards reference metrics by key
and cannot express an aggregation of their own.

The failure this prevents is not a wrong query. It is two correct queries disagreeing, which
destroys trust in every number the layer produces.

## 5. Queries are templates

Typed parameters, declared bounds, a permitted set of grouping dimensions, and a required scope
([ADR-0035](../design/adr-0035-no-free-form-queries.md)). There is no path from a caller's string
to a query plan, so there is nothing to sanitise.

The grouping restriction is the part that is not about injection: a valid, parameterised,
perfectly safe query grouping by three-digit postal, birth year, and sex singles out a person
without any injection at all.

## 6. Disclosure control runs in the executor

Primary suppression, then **complementary** suppression, then totals
([ADR-0036](../design/adr-0036-complementary-suppression.md)). A suppressed cell recoverable by
subtracting the published cells from a published total is not suppressed, and that is the first
thing anyone tries.

It runs in the executor rather than the display layer because the display layer sees one chart
and suppression is a property of the whole result set.

## 7. Freshness is part of every answer

Every result carries the ETL run that produced its data, the watermark reached, and the age of
that watermark. A dashboard that does not say how stale it is invites a decision on last week's
numbers, and the person making the decision has no way to know.

A result whose data is older than the metric's declared freshness requirement is returned
**marked stale**, not hidden — hiding it produces an empty dashboard with no explanation, which
is worse.

## 8. Dashboard categories

The four from the Phase 0 design, unchanged:

| Category | Answers | Consumer |
|---|---|---|
| Clinical / pharmacovigilance | Adverse-event signal trends, drug–condition co-occurrence, cohort sizing | Pharmacovigilance, medical affairs |
| Operational | Ingestion throughput, extraction accuracy, retrieval latency, index freshness | Platform engineering |
| Governance | Access summaries, break-glass review, de-identification job status | Compliance, security |
| Usage | Query volume, grounding pass rate, adoption by role | Product |

Each is a composition of metric keys and a layout. A dashboard contains no computation.

## 9. What this deliberately is not

**Not a warehouse product.** The dimensional model, ETL semantics, and query contract are
implemented and tested in process. A BigQuery or Synapse deployment must satisfy the same
contract; none has been built.

**Not differential privacy.** Deterministic cell suppression, whose limits are stated in
[ADR-0036](../design/adr-0036-complementary-suppression.md).

**Not a BI tool.** No visual designer, no ad hoc exploration. That is the deliberate consequence
of the template-only surface.
