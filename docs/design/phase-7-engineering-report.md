# Phase 7 Engineering Report — Analytics Warehouse & Self-Service Reporting

**Scope delivered:** a dimensional warehouse (7 facts, 6 conformed dimensions), a watermarked
idempotent ETL that de-identifies at load, a declarative semantic layer of 18 metrics, a
template-only query surface with typed parameters, statistical disclosure control with
complementary suppression, the four Phase 0 dashboard categories, scheduled report generation
with delivery, and the `/analytics/*` API the Phase 0 OpenAPI specification declared.

**Verification:** 1,194 tests collected — 1,182 pass, 12 skip (integration tests needing live
PostgreSQL/MongoDB/Neo4j, OCR needing Tesseract). 89 are new in Phase 7. `ruff format`,
`ruff check`, and `pyright` clean. Phases 1–6 consumed unchanged; no previous phase was modified.

---

## 1. Research applied

| Source | Applied as |
|---|---|
| Kimball dimensional modelling: conformed dimensions, one declared grain per fact | The star schema, with grain declared on `FactTable` and checked |
| Additive / semi-additive / non-additive measures | `Additivity` on every measure, enforced — a `RATIO` refuses to roll up |
| Statistical disclosure control: primary suppression is defeated by subtraction | Complementary suppression, then total suppression, then refusal ([ADR-0036](adr-0036-complementary-suppression.md)) |
| Small-cell thresholds in health statistics (commonly 11) | `DisclosurePolicy.minimum_cell_size`, configurable because jurisdictions differ |
| Quasi-identifier combination attacks (postal + birth year + sex) | A per-request budget on quasi-identifying groupings, independent of who is asking |
| HIPAA Safe Harbor, as implemented in Phase 6 | Reused at load: age bands with a 90+ cap, three-digit postal with restricted-area suppression ([ADR-0033](adr-0033-deidentify-at-load.md)) |
| Semantic-layer practice (a metric defined once, referenced everywhere) | The metric registry ([ADR-0034](adr-0034-metric-is-a-definition.md)) |
| Watermarked incremental ETL; wall-clock watermarks skip records written during clock skew | Source-cursor watermarks with a declared ordering kind |
| Phase 0's own commitment to parameterised RBAC-scoped templates | [ADR-0035](adr-0035-no-free-form-queries.md), implemented rather than restated |

## 2. Architecture summary

`extract → de-identify → conform → load → declare → template → suppress → present`.

Eight layers, dependency direction enforced by test. Three organising decisions:

**De-identify at load, not at query.** The warehouse contains no direct identifiers because
nothing writes them there. A query-layer bug then returns badly aggregated de-identified data —
an accuracy incident, not a privacy one.

**A metric is a declaration.** Not a query written wherever it was first needed. The failure this
prevents is not a wrong number; it is two correct numbers that disagree, which destroys trust in
the whole layer.

**Disclosure control lives in the executor.** Not the display layer, because suppression is a
property of the whole result set and a chart only ever sees its own slice.

## 3. Files created

**Service** (`services/analytics/src/cip_analytics/`): `domain.py`, `warehouse.py`, `etl.py`,
`semantic.py`, `disclosure.py`, `query.py`, `boards.py`, `reports.py`, `api.py`, `demo.py`,
`__init__.py`, `metrics/catalogue.yaml`, `README.md`.

**Docs**: `docs/architecture/11-analytics-warehouse.md`, ADR-0033 through ADR-0036, this report.

**Tests**: `tests/analytics/test_analytics.py` (89 tests).

## 4. Files modified

`pyproject.toml` and `pyrightconfig.json` (register the new package — additive only),
`README.md` and `docs/roadmap/implementation-roadmap.md` (record Phase 7 and renumber the
remaining phases). **No Phase 0–6 source file was changed.**

## 5. Major engineering decisions

**One template per metric, enforced.** Two templates exposing one metric means a caller naming
the metric gets whichever the lookup finds first; if their scopes differ that is a silent
privilege escalation. Varying the metric is the supported way to vary the surface.

**Sensitivity is contextual.** A column's sensitivity is interpreted against the fact it is
grouped on: `dim_date.month` identifies nobody on a job run and narrows a person down on an
observation. Modelling it as a column-only property made every operational metric ungroupable by
time — which is a broken dashboard, not a privacy control.

**Freshness is returned, never hidden.** A result older than its metric tolerates comes back
marked stale with its age. Hiding it produces an empty dashboard with no explanation.

**A failed tile does not blank its dashboard.** One refused panel — often refused for a
disclosure reason the viewer needs to see — should not cost the other seven.

**Health distinguishes "up" from "loaded".** An analytics service with an empty warehouse
answers every question with zero, and zero reads as a real finding. `health()` returns
`503 warehouse-empty` rather than `200`.

## 6. Bugs found

All four found by the end-to-end run or the adversarial pass; none by unit tests written
alongside the code.

### Blocker

**B1 — Disclosure control counted rows, not people.** A patient-level metric that declared no
`subject_column` suppressed on its row count. Twenty observations from three patients reported
20 subjects, passed a threshold of 11, and published a cell backed by three people. The threshold
is the entire control, and it was measuring the wrong quantity.

### High

**H2 — Quasi-identifier budget made operational metrics ungroupable by time.** `dim_date.month`
is declared quasi-identifying, correctly, and the budget counted it on every fact. Governance and
usage metrics carry a budget of zero, so **five of twenty dashboard tiles failed** — every panel
grouped by month. The underlying error was treating sensitivity as a property of a column alone
rather than of a column in the context of a fact.

**H3 — A numeric watermark wedged the pipeline permanently.** Cursors were compared lexically, so
`"10" < "9"` and the watermark refused to advance past 9. The pipeline then made no progress, and
because the loader is idempotent the symptom was silence rather than an error. The docstring
already promised "the source's declared comparator"; the code did not have one.

**H4 — Two templates for one metric escalated scope.** The API resolves a template from a metric
key and took the first match in sorted order. Registering a permissive template alongside a
restrictive one for the same metric let an analyst read a governance metric with an
`analytics:read` scope. Confirmed with a probe returning `200` where `403` was required.

## 7. Bugs fixed

All four, each with a regression test named for the attack.

- **B1**: `MetricRegistry.register` refuses a metric on a patient-level fact that declares no
  subject column, and the message states the arithmetic that makes it unsafe.
- **H2**: `FactTable.is_patient_level`, and the budget only counts quasi-identifiers when the
  fact is about people. All twenty tiles now render.
- **H3**: `CursorKind` with `LEXICAL` and `NUMERIC` orderings, declared per watermark. A genuine
  regression is still refused; a numeric advance from 9 to 10 now succeeds.
- **H4**: `TemplateRegistry.register` refuses a second template for a metric already exposed.

## 8. Regression tests added

**89 tests.** The load-bearing ones:

- `TestDisclosureControl` — the 87 − 42 − 38 = 7 attack, total suppression when nothing else can
  be withheld, refusal when a policy forbids withholding the total, and that a non-additive
  measure gets only primary suppression.
- `TestModuleBoundaries` — the layer map, no cross-service imports, no write path to the
  warehouse from the API, and that presentation layers never touch `MeasureKind`, `Cell`, or
  disclosure control.
- `TestSemanticLayer::test_a_patient_level_metric_without_a_subject_column_is_refused` (B1).
- `TestEtl::test_a_numeric_cursor_advances_past_nine` (H3).
- `TestQueryTemplates::test_two_templates_for_one_metric_are_refused` (H4).
- `TestTenantIsolation` — disjoint scans, and a principal without an organisation refused.
- `TestResourceBounds` — bounded load runs with failures retained preferentially, bounded report
  runs, and a scan cap.

Two boundary tests failed on their first run and both were correct signals: `reports` imported
`boards` sideways (the layer map was wrong — a report renders a dashboard, so it sits above it),
and the aggregation check was flagging row counts in `statistics()` dictionaries. The second was
a badly written test and was replaced with one that checks what the presentation layers may
touch.

## 9. Benchmarks

In-process, no database.

| Operation | Rate | Per operation |
|---|---|---|
| Disclosure control (59 cells) | 19,214 /s | 0.05 ms |
| ETL load | 11,593 rows/s | 0.09 ms |
| Query: count grouped by dimension | 2,692 /s | 0.37 ms |
| Query: p95 grouped by source | 461 /s | 2.17 ms |
| Query: ratio grouped by month | 225 /s | 4.45 ms |
| Dashboard render (5 tiles) | 109 /s | 9.14 ms |

Peak memory 5.6 MB for 4,559 facts and every query in the run.

## 10. Performance impact

**On existing phases: none.** No Phase 0–6 file was modified, and nothing in Phase 7 is on any
existing request path. The warehouse is loaded on a schedule and read separately, which was the
point of the Phase 0 design — dashboard load never contends with the query path's latency
budget.

The ratio and percentile queries are the slow ones because both scan the full fact set twice —
once for the numerator, once for the denominator — with no index. That is the honest cost of an
in-memory columnar scan and is the first thing a real warehouse's query planner would remove.

## 11. Security review

**Strong.** Every disclosure path is narrow by construction: no free-form query, no write path
from the API, no direct identifiers in the store, tenant scoping required by the row iterator's
signature, and suppression in the executor rather than any surface.

**Verified by probe:** a governance metric refused to an analyst; a `sql` parameter refused; a
quasi-identifier grouping refused; a 26-year range refused; a metric with no subject column
refused at registration; a second template refused; cross-tenant scans disjoint.

**Residual risks, stated rather than solved:**

- **Differencing across queries.** Two range queries differing by one day still difference. Cell
  suppression does not defend against it and ADR-0036 says so.
- **The pseudonymisation salt is process configuration.** Rotating it re-keys the warehouse and
  breaks longitudinal joins; no rotation procedure exists yet.
- **Report subscriptions are administrative.** A report runs as a declared principal, but nothing
  in this phase governs who may create one — that belongs with the admin API.
- **`InMemoryDelivery` is not a transport.** Real delivery to a mailbox or bucket is
  deployment-specific and unimplemented, and the `DeliveryChannel` protocol has no default so a
  deployment that forgets discovers it in configuration.

## 12. Production readiness

**Ready:** the model, the ETL semantics, the semantic layer, the query contract, and the
disclosure control. All are production-grade, tested, and honest about their limits.

**Not ready, in order:**

1. **The loaders are not wired to the live stores.** They demonstrate the contract against
   generated extracts. Until they read the Phase 1–6 systems the warehouse is empty, which
   `health()` reports rather than hiding.
2. **No warehouse product behind it.** In-memory columnar. A BigQuery/Synapse/Redshift
   implementation must satisfy the same contract and none has been built.
3. **No scheduler runtime.** `ReportScheduler.due()` decides what should run; something must call
   it. Phase 4's worker topology is the natural host and the wiring is not done.
4. **No UI.** The API returns JSON. A dashboard consumer is assumed and does not exist.
5. **Metric definitions are unreviewed.** The 18 shipped metrics are plausible and exercise the
   machinery; the clinical and pharmacovigilance ones have not been reviewed by a specialist.
6. **Suppression thresholds are defaults.** Eleven is a common figure in US health statistics and
   is not a decision this platform can make for a deployment.

**Assessment:** Phase 7 delivers the analytics layer the Phase 0 design specified, with the
security property that design insisted on actually implemented rather than restated. The gap is
integration, not engineering — the same shape as Phase 6's, where the machinery is sound and has
not met the systems it will read.

The most valuable finding was not any single bug but the pattern behind three of them: **B1, H2,
and H4 were all cases where a property was modelled at the wrong scope.** Sensitivity as a
property of a column rather than a column-in-a-fact; subject count as a property of the query
rather than the metric; scope as a property of the template rather than the metric-plus-template
pair. Each was individually correct code that composed into something wrong.

## 13. Technical debt

| Item | Why it matters | Next |
|---|---|---|
| Loaders not wired to live stores | The warehouse is empty | Next |
| No warehouse backend | In-memory does not survive a restart | With infrastructure |
| No scheduler runtime | Reports run only when called | Wire to Phase 4 workers |
| Full scan per query | Ratio and percentile scan twice | With a real query planner |
| No salt rotation procedure | Rotation breaks longitudinal joins | Before production |
| Metric definitions unreviewed | Plausible, not validated | With analysts |
| No differencing defence | Stated limit of cell suppression | Query-budget accounting, if needed |
| Report creation ungoverned | Anyone with admin access sets the principal | With the admin API |

## 14. Remaining work

Phase 8 (enterprise hardening and compliance certification), Phase 9 (analytics UI and scale),
Phase 10+ (explicitly out of scope) — see the
[roadmap](../roadmap/implementation-roadmap.md).

Nearest-term, in dependency order: wire the loaders to the Phase 1–6 stores; host the scheduler
on the Phase 4 worker topology; put a warehouse backend behind the store contract; then a UI.

## 15. Completion estimate

Roughly **75%** of the platform as scoped in Phase 0.

Delivered: document intelligence, retrieval and knowledge graph, clinical copilot, production
platform, clinical decision intelligence, ecosystem interoperability, and analytics — seven of
the nine planned phases.

Remaining: enterprise hardening and third-party compliance attestation (Phase 8), the web UI and
scale work (Phase 9). The figure is honest about a real caveat: **almost nothing has run against
real infrastructure or a real counterparty**, so the remaining 25% is weighted toward
integration and validation rather than new code, and it is the harder 25%.

## 16. Suggested next phase

**Wire the platform to itself, then harden it.**

Phase 7 completed the last major *capability*. Every remaining risk in this report and the last
two is an integration or validation risk, not a missing feature. The most valuable next phase is
therefore the one that closes them:

1. **Wire the analytics loaders to the Phase 1–6 stores**, so the warehouse holds real platform
   data rather than generated extracts. Small, and it turns four dashboards from a demonstration
   into an operating tool.
2. **Host the schedulers and workers**: report scheduling, ETL runs, and Phase 4's background
   jobs on one runtime.
3. **Stand the stack up against real infrastructure** — Postgres, MongoDB, Neo4j, Redis, a broker
   — and run the integration tests that have been skipping since Phase 1. Twelve skipped tests
   have been carried for six phases and each is a claim nobody has checked.
4. **Then** Phase 8's compliance hardening, which needs a running system to assess.

Deferring the UI is deliberate: a dashboard over an empty warehouse demonstrates nothing, and
the API contract is already stable enough to build against once there is data behind it.
