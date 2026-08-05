# Analytics Warehouse & Self-Service Reporting

Phase 7. A dimensional warehouse fed by a de-identifying ETL, a declarative metric layer, a
template-only query surface with statistical disclosure control, four dashboard categories, and
scheduled reports.

Realises the Phase 0 design in
[05-analytics-dashboard.md](../../docs/architecture/05-analytics-dashboard.md), including the
commitment it made: parameterised, RBAC-scoped query templates only, no free-form SQL.

## Not the same thing as Phase 6's dashboards

| | Phase 6 `cip_interop.dashboards` | Phase 7 `cip_analytics` |
|---|---|---|
| Question | "What is happening right now?" | "What happened, and how does it compare?" |
| Source | The live event stream | A warehouse loaded on a schedule |
| Window | Minutes, sliding | Years, dimensional |
| History | None — it forgets | The point of it |
| Consumer | Operators watching an interface | Analysts asking a question |

They stay separate. Real-time telemetry that answers historical questions grows unbounded
state; a warehouse that answers real-time questions is always stale.

## Pipeline

```
  operational store ─┐
  FHIR repository  ──┤
  event stream ──────┼─► ETL ──► de-identify ──► conform ──► load ──► ★ warehouse
  audit log ─────────┤    (watermarked,          (Safe        (star
  copilot telemetry ─┘     incremental,           Harbor)      schema)
                           idempotent)                            │
                                              ┌───────────────────┘
                                              ▼
                                   semantic layer  ── metrics declared once
                                              ▼
                                   query templates ── typed parameters only
                                              ▼
                              disclosure control ── primary + complementary
                                              ▼
                      dashboards · scheduled reports · /analytics/*
```

## What cannot happen, structurally

**A direct identifier in the warehouse.** The ETL removes them on the way in and nothing else
writes there ([ADR-0033](../../docs/design/adr-0033-deidentify-at-load.md)). A query-layer bug
therefore returns badly aggregated de-identified data — an accuracy incident, not a privacy one.

**Two dashboards disagreeing about one number.** A metric is a declaration with a key and a
version; dashboards reference it by key and cannot express an aggregation
([ADR-0034](../../docs/design/adr-0034-metric-is-a-definition.md)). A boundary test asserts the
presentation layers never touch `MeasureKind`, `Cell`, or the disclosure control.

**A free-form query.** Typed templates with declared parameters, bounds, permitted groupings,
and a required scope ([ADR-0035](../../docs/design/adr-0035-no-free-form-queries.md)). There is
no path from a caller's string to a query plan, so there is nothing to sanitise.

**A suppressed cell recoverable by subtraction.** Primary suppression, then complementary, then
the total — and a refusal when no combination is safe
([ADR-0036](../../docs/design/adr-0036-complementary-suppression.md)).

**Suppression counted on rows rather than people.** A patient-level metric must declare its
subject column; the registry refuses one that does not, because twenty observations from three
patients would otherwise pass a threshold of eleven.

**A metric reachable through two templates.** Refused at registration: a caller naming the
metric would get whichever was found first, escalating to the more permissive scope.

**An unscoped scan.** The warehouse's row iterator requires an organisation, so a cross-tenant
read is unwritable rather than merely discouraged.

**A silent duplicate load.** Every loader declares a natural key, so a rerun deduplicates. A
rerun that doubled every row would look like a business trend.

## Modules

Layer *n* imports only layers below it, enforced by a test.

| Layer | Module | Responsibility |
|---|---|---|
| 0 | `domain` | Measures, additivity, freshness, disclosure policy, scopes |
| 1 | `warehouse` | Star schema and a typed, tenant-scoped store |
| 2 | `etl`, `semantic`, `disclosure` | Loading, metric declarations, suppression |
| 3 | `query` | Templates, execution, and where disclosure control runs |
| 4 | `boards` | The four dashboard categories |
| 5 | `reports` | Schedules, rendering, delivery |
| 6 | `api` | `/analytics/*` |
| 7 | `demo` | The end-to-end run |

## The star schema

Seven facts — encounter, observation, adverse event, document ingestion, answer, PHI access, job
run — over six conformed dimensions: date, organisation, cohort, code, actor role, source system.

Two rules are enforced rather than documented, because both failures stay invisible until two
dashboards disagree:

- **One declared grain per fact.** `fact_observation` is one row per observation, not per
  patient.
- **Declared additivity per measure.** A rate is recomputed at the level it is grouped, never
  summed from finer rows.

Sensitivity is interpreted **in the context of the fact**: `dim_date.month` is a quasi-identifier
on an observation and is not one on a job run. Without that, either every operational metric
becomes ungroupable by time or every patient metric becomes groupable by a quasi-identifier.

## Metrics

18 shipped across the four categories, in
[`catalogue.yaml`](src/cip_analytics/metrics/catalogue.yaml). Each declares a key, version,
grain, measure, filters, an optional denominator, a disclosure policy, and a freshness
tolerance.

Every result carries its lineage — metric key, version, fact, filters, grouping — and its
freshness, so a chart is reproducible and its age is visible.

## Running it

```bash
python -m cip_analytics.demo
```

Loads 4,559 synthetic facts through the ETL, demonstrates idempotency, runs every metric, shows
what the query surface refuses, attacks the disclosure control by subtraction, renders all four
dashboards, produces two scheduled reports, verifies tenant isolation, exercises the API, and
benchmarks each layer. See the
[Phase 7 engineering report](../../docs/design/phase-7-engineering-report.md).

## What is deliberately not real yet

**Not a warehouse product.** The dimensional model, ETL semantics, and query contract are
implemented and tested in process. A BigQuery, Synapse, or Redshift deployment must satisfy the
same contract; none has been built.

**Not differential privacy.** Deterministic cell suppression, whose limits are stated in
ADR-0036 — it does not defend against differencing across many correlated queries.

**Not a BI tool.** No visual designer, no ad hoc exploration. That is the deliberate consequence
of the template-only surface, not an omission.

**Sources are not yet wired to the live systems.** The loaders demonstrate the contract against
generated extracts. Connecting them to the Phase 1–6 stores is a deployment task, and the
warehouse is empty until it is done — which `health()` reports as `503 warehouse-empty` rather
than answering every question with zero.
