# Phase 9 — Research, Gap Analysis, and Engineering Roadmap

**Status:** Proposed. Awaiting approval. **No implementation code has been written.**

**Repository:** `github.com/vinayakarya02/clinical-intelligence-platform` @ `33797de`

---

## 0. Repository verification

Performed before any analysis. Every figure below was measured, not recalled.

| Check | Result |
|---|---|
| Branch | `main` (default on GitHub) |
| HEAD | `33797def80f5d3f98bd09f3a4752412c95e1cb0b` |
| Remote | `origin` → `https://github.com/vinayakarya02/clinical-intelligence-platform.git` |
| Local vs remote | **identical** — 0 uncommitted, 0 unpushed |
| CI status | **success** — 7/7 jobs on the current HEAD |
| Commits | 17 (9 phase commits + 8 post-publication fixes) |
| Tracked files | 407 |
| Documentation | 81 markdown files · 39 ADRs · 12 architecture docs · 9 runbooks · 9 phase reports |
| **Broken links** | **0** (an initial hit was my checker's regex truncating `.env.example`) |
| **TODO / FIXME / XXX / HACK** | **0 repo-wide** — genuinely unusual, and to the codebase's credit |
| Skipped tests | 8 sites, all with stated reasons; 0 `xfail` |
| Failing benchmarks | None — because **no benchmark asserts anything** (see M-8) |
| Test suite | 1,142 test functions across 40 files; 1,235 collected including parametrisation |

**Repository integrity is sound.** The problems below are not disorder; they are the gap between a system that is internally correct and one that has met production.

---

## 1. Research summary

Sources are current as of August 2026. Two findings changed my assumptions and are flagged.

### 1.1 OpenTelemetry GenAI conventions — *not stable*

As of the v1.42.0 release (12 June 2026) all `gen_ai.*` attributes and spans **moved out of the
main semantic-conventions repository into a dedicated GenAI repository**, and as of July 2026
**no GenAI span, metric, or attribute is marked Stable** — the conventions remain in Development.

Load-bearing names: spans `invoke_agent`, `chat`, `execute_tool`; the required metric
`gen_ai.client.operation.duration`; the recommended `gen_ai.client.token.usage`; attributes
`gen_ai.request.model`, `gen_ai.usage.input_tokens`.

**Implication for us:** adopt the conventions, but pin the semconv package version and isolate
attribute construction behind one module. Instrumenting 40 call sites against a Development-status
spec is how a convention change becomes a 40-file diff.

### 1.2 FDA CDS guidance was replaced in January 2026 — *and this repository is already current*

On **6 January 2026** the FDA issued an updated final guidance replacing the September 2022
version. Criterion 4 — the healthcare professional must be able to **independently review the
basis** for a recommendation — is now the centre of gravity, with a new emphasis on avoiding
time-critical black-box reliance. The previous requirement to output *multiple* recommendations
has relaxed to enforcement discretion where only one option is clinically appropriate.

I expected to find the repository citing the superseded 2022 guidance. **It does not.**
`docs/architecture/07-clinical-copilot.md:20`, `docs/design/adr-0022-deterministic-decisions.md`,
and the Phase 3 and Phase 5 reports all cite the January 2026 guidance, and the design response is
correct: explanation is a *pipeline stage*, not a rendering option, and citation and provenance are
*constructor invariants* on `Recommendation`.

**This is the single strongest area of the codebase and needs no Phase 9 work.** I record it
because a gap analysis that only lists deficits misrepresents the system.

### 1.3 Comparison against production platforms

Used to locate architecture, **not** to propose replacing what works.

| System | What it does | This platform | Verdict |
|---|---|---|---|
| **Microsoft GraphRAG** | Leiden community detection → hierarchical community summaries → *global* search over themes | Entity/relation extraction + traversal; **no community detection, no global search** | **Gap.** "What are the emergent themes across this cohort" is unanswerable. Local/entity search works. |
| **Azure AI Search** | BM25 + vector + **RRF**; semantic ranker (cross-encoder); index **aliases** for zero-downtime reindex | BM25 + vector + RRF ✓ *(architecturally equivalent)*; `FeatureReranker` linear scorer, not a cross-encoder; **no reindex/aliasing strategy** | Fusion matches. Reranker and reindex are gaps. |
| **MS Fabric Healthcare / OneLake** | Medallion bronze→silver→gold; FHIR → Delta; time travel | raw→parsed→warehouse is the same shape; **de-identify-at-load is stronger than Fabric's default** | Shape matches; no columnar/lakehouse format, no time travel. |
| **Google Vertex AI Search** | Grounded generation with citations; check-grounding API | Citations are **constructor invariants** — arguably stricter | Strong. No grounding-confidence score. |
| **AWS HealthLake** | Managed FHIR R4 + integrated NLP + SMART | FHIR R4 ✓, SMART on FHIR ✓, **plus a full EMPI HealthLake lacks** | Functionally ahead — but **not persisted**. |
| **LangGraph** | Stateful graph orchestration, **checkpointing**, durable resume, human-in-the-loop interrupts | `ClinicalPipeline`: linear 10-stage async sequence, cooperative cancellation, **no checkpointing, no resume, no HITL interrupt** | **Gap.** A failure at stage 9 re-runs stages 1–8. |
| **OpenTelemetry GenAI** | `gen_ai.*` spans and metrics | **Zero OpenTelemetry anywhere** — not even a dependency | **Gap.** |
| **Neo4j production** | Causal clustering, read replicas, index/constraint management | `Neo4jGraphStore` exists and is **instantiated only in a test** | **Gap.** |
| **MongoDB Atlas Vector Search** | `$vectorSearch`, HNSW ANN, filters, quantization | `MongoAtlasVectorStore` exists and is **instantiated only in a test** | **Gap.** |
| **FHIR R4 / SMART / HL7 v2** | Conformance, scopes, MLLP framing, ACK semantics | Implemented to a genuinely high standard | **Strength.** |

**Summary judgement:** the *architecture* is competitive with these platforms and in places
(consent, de-identification, EMPI, citation discipline) exceeds them. The *operational substrate*
does not exist. This platform is an excellent design with a missing runtime.

---

## 2. Gap analysis

Classified by production consequence. Each entry gives why it matters, the blast radius, the
files, and what it blocks.

### BLOCKERS — the platform cannot be deployed

---

#### **B-1. Three replicas, three divergent platforms**

**Why:** `deploy/k8s/10-api-deployment.yaml:10` sets `replicas: 3`. Every stateful component in
the composition root is a process-local dictionary: `InMemoryVectorStore`, `InMemoryGraphStore`,
`EmpiIndex()`, `RepositoryRegistry()`, `ConsentEngine`, `EventStream(partitions=8)`,
`Warehouse(schema)` — all in `services/gateway/src/cip_gateway/platform.py`.

**Impact — worse than data loss.** This is not "state is lost on restart". Three pods behind one
Service each hold a *different* platform. A consent revocation routed to pod 1 leaves pods 2 and 3
still disclosing. A patient identity merge on pod 2 is invisible to the others. The failure is
non-deterministic, invisible to health checks, and **presents as a privacy incident**, not as an
outage.

**Files:** `platform.py:117-128, 164-166, 233`; `deploy/k8s/10-api-deployment.yaml:10`

**Chain:** blocks W1 → W2 → W6 → W8. Nothing about persistence, isolation, or performance can be
validated until this is closed.

---

#### **B-2. The disclosure audit trail does not persist**

**Why:** `AuditSink` (`services/interop/src/cip_interop/consent.py:257`) is a Protocol with
**exactly one** implementation — `InMemoryAuditSink` (`:268`), a bounded list whose own docstring
says *"for tests and single-process runs."*

**Impact:** every consent decision, purpose-of-use evaluation, and break-glass access is written to
a buffer that is dropped oldest-first and vanishes on restart. `docs/architecture/06-security-compliance.md`
commits to HIPAA §164.312(b) audit controls. **The audit trail is the one artefact that must
outlive the process, and it is the one that doesn't.** Combined with B-1, a break-glass access on
pod 2 leaves no trace anywhere the other pods can see.

Phase 1 has a real `audit_log` table and repository — a *different* audit system, serving
ingestion. The two were never joined.

**Files:** `consent.py:257,268`; `platform.py:90-92`; `libs/cip_core/src/cip_core/models/tables.py:348`

**Chain:** independent of B-1 in principle; in practice both need the same PostgreSQL wiring (W0).

---

#### **B-3. Two applications, one database layer, no overlap**

**Why:** `libs/cip_core/db/` holds real, working `PostgresManager` (with RLS-scoped
`tenant_session`), `MongoManager`, and `Neo4jManager`. They are constructed in **one place** —
`services/ingestion/src/cip_ingestion/api/dependencies.py:65-67` — which belongs to the *Phase 1
ingestion app*, a separate FastAPI application. Phase 8's composition root constructs **zero**
persistence backends.

| | Phase 1 ingestion app | Phase 8 unified app |
|---|---|---|
| Real PostgreSQL / Mongo / Neo4j | **yes** | no |
| Container, startup validation, unified surface | no | **yes** |
| Serves FHIR, analytics, decision, copilot | no | yes (in-memory) |

**Impact:** neither application is deployable. Phase 8 integrated the *services* and left the
*storage layer* in Phase 1 — a gap the demo hides, because the demo runs the unified app and the
unified app never needed a database.

**Files:** `cip_ingestion/api/dependencies.py:65-67`; `cip_gateway/platform.py` (entire)

**Chain:** **W0 gates everything.** There is nowhere to put a connection until these are one app.

---

#### **B-4. Production configuration names three backends that do not exist**

**Why:** `PlatformSettings` correctly refuses `cache.backend == "memory"` and
`queue.backend == "memory"` in deployed environments, so `deploy/k8s/01-configmap.yaml` sets
`redis`, `celery`, `kafka`. But:

- `RedisCache` (`libs/cip_platform/src/cip_platform/cache/redis_cache.py:38`) is real and
  **instantiated nowhere** — no factory chooses between it and `InMemoryCache`
- **Celery and Kafka backends do not exist at all.** `libs/cip_platform/tasks/` and `events/`
  contain `base.py` and `memory.py` and nothing else

**Impact:** production configuration describes a system nobody built. Phase 8's startup validation
passes because it checks the settings are *coherent*; nothing asks whether they are *satisfiable*.

**Files:** `cache/redis_cache.py:38`; `cache/__init__.py`; `tasks/`; `events/`; `01-configmap.yaml`

**Chain:** blocks W5 (rate limiting) and the async path.

---

### HIGH — the platform cannot be operated or shipped

---

#### **H-1. Observability is fully built and not connected**

`MetricRegistry.render()` emits Prometheus text format (`metrics.py:175`). There are **9 alerts**
(`observability/prometheus/alerts.yml`), a complete Grafana dashboard, and **9 runbooks** — one per
alert. There is **no `/metrics` endpoint**. Prometheus is configured to scrape an endpoint that does
not exist, to fire alerts on metrics nothing publishes, into runbooks nobody can action.

No `ServiceMonitor` exists either, so a Prometheus Operator install would not discover the target
even once the endpoint is added.

**Distributed tracing is worse:** `TelemetryPolicy` has `tracing_enabled`, `trace_sample_ratio`,
`otlp_endpoint`; the ConfigMap sets a collector address. `grep -rn "opentelemetry"` over `libs/`
and `services/` returns **nothing**. Phase 8 threaded a correlation id through all ten pipeline
stages; it reaches logs and no trace backend.

**Files:** `observability/`, `metrics.py:175`, `01-configmap.yaml`, `routes.py`

---

#### **H-2. No way to ship the image**

`.github/workflows/ci.yml` builds and scans; `push: false`. No registry, no signing, no retained
attestation, no environment, no deploy. The nine manifests have never been applied to a cluster,
and they reference `ghcr.io/example/cip:0.4.0` — a path that does not exist.

CI triggers on `push: [main]`, `pull_request`, `workflow_dispatch`. **There is no tag trigger and
no release workflow.**

---

#### **H-3. Rate limiting is per-replica**

`TokenBucketLimiter` is in-process. With `replicas: 3`, the configured 600/min per tenant is
enforced **1800/min in aggregate**. A limit that loosens as you scale is loosest exactly when it is
most needed. Phase 8's startup validation already warns about this — the warning becomes
unnecessary once the limiter is shared.

---

#### **H-4. Real-infrastructure test coverage is 0.9%**

**10 of 1,142 tests** touch real infrastructure, all in
`tests/integration/test_postgres_integration.py`. CI starts PostgreSQL, Redis, **and Neo4j**
services — **Redis and Neo4j are never contacted by a single test.** MongoDB has no service at all,
so the Atlas vector path (the production vector store per ADR-0009) has never executed.

RLS is proven for PostgreSQL — as of this session, and only after fixing a superuser bypass that
had made every prior run vacuous. The equivalent isolation guarantee in MongoDB, Neo4j, and Redis
is asserted by architecture documents and by nothing else.

**Resolved in W6, and it was worse than this entry recorded.** The 10 tests were not merely a thin
slice — they had **never executed**. The fixture's teardown dropped the migrated schema, every
subsequent test errored, and each error surfaced through a fixture that called `pytest.skip`, so
the job printed "10 skipped" and exited 0. The RLS tests also created their own policy before
asserting on it, so even a run that did execute tested a policy the test had just written rather
than the migration's.

CI now starts all five services and runs 55 tests against them, with a tenant-isolation test per
backend. A skip means the infrastructure is absent and nothing else; any skip naming an
unreachable service fails the run. See
[docs/testing/integration-testing.md](../testing/integration-testing.md).

---

#### **H-5. Four declared routes return 501, and the Phase 8 check misses it**

`app.py:267` returns `501 not implemented` for any service without an HTTP adapter. Only `interop`
and `analytics` have one. The registry declares `/v1/search`, `/v1/ask`, `/v1/documents`,
`/v1/documents/{document_id}`.

The registry's dead-route check asks whether the backing *service is registered* — and `retrieval`
is. It never asks whether an *adapter exists*. **That is a hole in a Phase 8 control**, and it is
mine; closing it belongs here.

---

#### **H-6. No circuit breaker, no transactional outbox**

Retry (84 sites), backoff, idempotency (41), dead-letter (62), and timeouts (27) are all present
and well-built. **Circuit breakers: 0. Bulkheads: 0. Outbox: 0.**

- Without a breaker, retry against a slow dependency becomes a retry storm — and W1 introduces four
  new remote dependencies.
- Without an outbox, W1 creates a **dual-write problem the moment a FHIR resource is persisted
  *and* an event published.** Deciding this after the persistence code is written means retrofitting
  it through every write path.

---

#### **H-7. `python-jose` is unmaintained and pulls an unfixable advisory**

Last release 2021. One import site (`services/ingestion/src/cip_ingestion/api/security.py:26`).
Pulls `ecdsa`, carrying PYSEC-2026-1325 with **no fix version**, currently suppressed in CI with
documented reasoning (the platform signs HS256 and verifies OIDC via JWKS — no ECDSA operation is
ever performed, so it is not reachable). Correct short-term call; the dependency should still go.

---

#### **H-8. Two manifest defects would break W5 on first deploy**

Found by reading the manifests rather than assuming them. Both are latent today because nothing
uses Redis — and both activate the moment W5 lands.

1. **The Redis StatefulSet has no persistent storage.** `deploy/k8s/20-stateful-redis.yaml:87-89`
   mounts `emptyDir` at `/data` — no PVC, no `volumeClaimTemplates`. A StatefulSet exists precisely
   to give stable storage, and this one has none. Rate-limit and cache state would not survive a
   pod restart.
2. **NetworkPolicy denies the traffic it means to allow.** `default-deny-all` (`:13`) selects all
   pods for Ingress and Egress. `app-egress-to-datastores` (`:74`) permits api/worker/scheduler
   *egress* to `cip-redis:6379` — but **no policy grants `cip-redis` matching *ingress***. On a CNI
   that enforces both directions, cache traffic is blocked.

Separately, the same file's datastore rule (`:100-108`) is **ports-only with no `to:` selector**,
so egress to *any* destination on 5432/27017/7687/443 is permitted. It is self-documented at
`:97-99`, so it is a known trade rather than an oversight — but "any host on 443" from a PHI-
handling pod should be an explicit `ipBlock` before production.

**Files:** `deploy/k8s/20-stateful-redis.yaml:87-89`, `deploy/k8s/30-networkpolicy.yaml:13,74,100-108`

---

#### **H-9. "What version is running?" has six answers**

| Value | Where |
|---|---|
| `0.1.0` | `pyproject.toml:7` (the distribution) |
| `0.1.0` | `cip_core/__init__.py:26` |
| `0.2.0` | `cip_retrieval/__init__.py:50` |
| `0.1.0` | `cip_ingestion/version.py:21` — **what `/health/live` and `/health/ready` return** |
| `0.5.0` | `cip_gateway/app.py:352` — **what the API reports to clients** |
| `0.4.0` | `deploy/k8s/*:61,35,38` — **the image tag actually deployed** |
| `0.1.0-phase0` | `docs/api/openapi.yaml:4` |

Nothing derives from anything else — no `importlib.metadata.version()` anywhere. The health
endpoint says `0.1.0`, the API says `0.5.0`, and the running image is `0.4.0`. During an incident,
"which build is this?" has three defensible and contradictory answers.

`PIPELINE_VERSION = 1.0.0` is the one deliberate exception, documented at `version.py:1-15` for
selective corpus reprocessing — that one is correct and should stay independent.

There are also **zero git tags** and **no CHANGELOG**, though `openapi.yaml:25` promises deprecation
tracking "in the platform changelog."

---

#### **H-10. The published API contract and the real one share 4 paths and disagree on 28**

`docs/api/openapi.yaml` (746 lines, hand-written, `info.version: 0.1.0-phase0`) is the Phase 0
*target* surface. `platform_routes()` is the real one. They were never reconciled.

- **In the spec, not implemented (10):** `/documents/batch`, `/documents/fhir-bulk-import`,
  `/documents/{id}/reprocess`, `/webhooks`, `/retrieval/query`, `/chat/sessions`,
  `/chat/sessions/{id}/messages`, `/access/break-glass`,
  `/admin/tenants/{id}/audit-log`, `/admin/tenants/{id}/offboard`
- **Implemented, undocumented (18):** all 8 FHIR routes, 6 of 8 analytics routes, `/v1/search`,
  `/v1/ask`, both health probes
- **`/metrics` is in neither** — yet `01-configmap.yaml:38` and the pod scrape annotations
  (`10-api-deployment.yaml:30`) both point at it

`/access/break-glass` and `/admin/tenants/{id}/audit-log` are the ones that matter: both are
documented compliance surfaces with no implementation. **No CI check compares the two.**

---

#### **H-11. Integration tests do not gate the image build**

`build.needs: [static, test, architecture, security, manifests]` (`ci.yml:257`) — **`integration`
is absent.** The only job that touches real infrastructure cannot block a release. Combined with
H-2 (nothing publishes yet) this is currently harmless; it becomes a hole the day W3 lands.

Also: two third-party actions are pinned to a moving ref — `instrumenta/kubeval-action@master`
(`:241`) and `aquasecurity/trivy-action@master` (`:295`). An unpinned action is arbitrary code
execution in a job holding `security-events: write`.

---

### MEDIUM

| # | Gap | Detail |
|---|---|---|
| **M-1** ✅ | **Coverage measures 6 of 9 packages** — *resolved in W6: all nine measured, `--cov-fail-under=75` against a current 77.8%.* | `--cov=` flags omit `cip_decision`, `cip_interop`, `cip_analytics` — **350 tests, 31% of the suite**, coverage never recorded. Flags were never updated as Phases 5–7 landed. No `fail_under` anywhere; nothing gates on coverage. `ci.yml:65` |
| **M-2** ✅ | **Two self-neutering tests** — *resolved in W6. The EMPI one had been skipping on every run since it was written: its pair scored 4.94 against a lower threshold of 6.0, so `decide_review` had never once executed.* | `tests/unit/test_parsers.py:143` skips based on `engine.calls == 0` — the *outcome of the code under test*. A real regression reports "skipped", not "failed". Same shape at `tests/interop/test_interop.py:825`. These tests cannot fail for the reason they exist. |
| **M-3** | **Embedding cache is off** | `EmbeddingService` defaults to `NullEmbeddingCache` (`service.py:99`) and `platform.py:119` passes no cache. Every embedding is recomputed. `InMemoryEmbeddingCache` appears only in a demo. |
| **M-4** | **No pipeline checkpointing** | `ClinicalPipeline` is linear with cooperative cancellation. A failure at stage 9 re-runs 1–8. LangGraph-style checkpointing would make long ingestion resumable. |
| **M-5** | **No reindex strategy** | Changing the embedding model invalidates every vector. Azure AI Search solves this with index aliases; there is no equivalent plan. Blocks ever replacing the baseline embedder. |
| **M-6** | **Redis is a single StatefulSet replica** | `20-stateful-redis.yaml:31` — `replicas: 1`. Once it backs rate limiting and caching (W5), it is a single point of failure. |
| **M-7** | **`ocr` marker is dead code** | Declared in `pyproject.toml:92`, skip logic built in `conftest.py:47-54`, **applied to no test**. |
| **M-8** | **Benchmarks cannot fail** | `scripts/benchmark_platform.py` prints and `return 0` unconditionally. Six demo modules have benchmark sections; none runs in CI, none asserts. Published timings in five phase reports have no automated guard. |
| **M-9** | **No ResourceQuota / LimitRange / PriorityClass** | Present: HPA, 2× PDB, 4× NetworkPolicy, ServiceAccounts, topologySpreadConstraints — genuinely good. Absent: namespace-level guards and scheduling priority. |
| **M-10** | **DR is documented, never exercised** | `docs/operations/sla-dr.md` states RPO/RTO per store and a cross-region failover runbook. No restore has been executed. A DR plan never run is a document, not a capability. |
| **M-11** | **No GraphRAG-style global search** | Community detection and hierarchical summarisation absent; cohort-level thematic queries unanswerable. |
| **M-12** | **No migration Job manifest** | `docker/entrypoint.sh:29` supports a `migrate` role, and no Kubernetes Job or Helm hook invokes it. Schema upgrades have no declared execution path in-cluster. |
| **M-13** ✅ | **Two divergent compose files** — *resolved in W6: the root Phase 1 file is deleted and the Makefile passes `-f docker/docker-compose.yml`. It also mattered more than "can drift": only Kafka published a host port, so the integration suite was unrunnable locally.* | `docker/docker-compose.yml` (full stack, password `devpassword`) and a root `docker-compose.yml` (Phase 1 only, password `change-me`). Two files that can drift; only one is referenced by the Makefile. |
| **M-14** | **No readiness probe on worker or scheduler** | `11-worker-deployment.yaml:63-64` documents the omission; `12-scheduler-deployment.yaml` has neither readiness nor startup probe. A wedged worker stays in the endpoint set. |
| **M-15** | **No Helm chart or Kustomize overlay** | Raw manifests with hard-coded values — no environment parameterisation. Staging and production cannot differ without editing tracked files. |
| **M-16** | **Base image pinned by tag, not digest** | `docker/Dockerfile:15-16` explicitly acknowledges this: "Pinned by digest in a real deployment." The scanned image and the shipped image can differ. |
| **M-17** | **No governance files** | No `dependabot.yml`, `CODEOWNERS`, `SECURITY.md`, `CONTRIBUTING.md`, PR template, issue templates, or branch-protection-as-code. For a public repository inviting scrutiny, `SECURITY.md` is the notable absence. |

### LOW

| # | Gap |
|---|---|
| **L-1** | One timing-based test (`tests/platform/test_platform.py:855`, `churn < fill * 10 + 0.5`) can fail from runner contention alone. Generously bounded and deliberate; worth watching. |
| **L-2** | Test concentration: `tests/interop/test_interop.py` holds 162 tests in one file; `tests/decision` 99 in two. |
| **L-3** | No `CHANGELOG`, no git tags, no dependabot, no CODEOWNERS, no PR/issue templates. |
| **L-4** | Node.js 20 deprecation warnings on `actions/checkout@v4` and `setup-python@v5`; CodeQL Action v3 deprecates December 2026. |
| **L-5** | No frontend of any kind. Dashboards are server-side render structures, not a UI. Correctly scoped out to date — but "dashboards" in the docs means data structures, and that should stay explicit. |

---

## 3. Risk assessment

| Risk | Likelihood | Severity | Mitigation |
|---|---|---|---|
| **W1 introduces a tenant-isolation defect in a new backend** | **High** | **Critical** | Write the isolation test *before* the persistence for each backend (see §7). RLS proved that a test written after the code encodes the code's assumptions. |
| Dual-write inconsistency between store and event stream | High | High | Decide the outbox pattern **before** W1, not during (H-6). |
| Retry storms once four remote dependencies exist | Medium | High | Circuit breakers land with W0, before the dependencies do. |
| OTel GenAI convention churn (Development status) | Medium | Low | Pin semconv; isolate attribute construction in one module. |
| Migration of six in-memory stores is larger than estimated | **High** | Medium | Sequence by blast radius: consent and audit first (compliance), warehouse last (rebuildable from source). |
| Scope creep into model integration | Medium | Medium | Explicitly out of scope; see Open Question 4. |
| A Phase 9 change breaks a green Phase 1–8 suite | Low | Medium | 1,235 tests currently pass; they run on every commit. Additive-only changes. |

---

## 4. Technical debt analysis

Debt here is unusually well-behaved and worth characterising accurately.

**What is *not* debt:** zero TODO/FIXME markers repo-wide. Every substitute implementation
documents itself honestly — `HashingEmbeddingProvider` says *"a baseline to beat, not a model to
ship"*; `Warehouse` says *"not a database; it is the contract a database must satisfy"*;
`streaming.py` says *"the semantics are the contract a Kafka-backed implementation must satisfy"*.
Deferred decisions live in ADRs, not in comments.

**This is deliberate scaffolding, not neglect** — and it is why W1 is tractable: the seams
(`EmbeddingProvider`, `Reranker`, `LanguageModel`, `AuditSink`, `VectorStore`, `GraphStore`
protocols) already exist. The in-memory implementations become *one* implementation of a port
rather than the only one.

**Actual debt, ranked:**

1. **Coverage flags frozen at Phase 4** (M-1) — 31% of tests uncounted. One-line fix, real signal loss.
2. **Two tests that cannot fail** (M-2) — worse than no test, because they report a green skip.
3. **Benchmarks that cannot fail** (M-8) — five phase reports publish timings nothing guards.
4. **Dead `ocr` gating mechanism** (M-7).
5. **`python-jose`** (H-7) — one import site, unmaintained since 2021.
6. **Registry dead-route check doesn't check adapters** (H-5) — a Phase 8 control with a hole in it.

---

## 5. Production readiness assessment

| Dimension | State | Verdict |
|---|---|---|
| Architecture & domain modelling | Nine services, enforced layering, composition root, 39 ADRs | **Ready** |
| Clinical correctness & regulatory posture | FHIR R4, SMART, HL7 v2, EMPI, consent, FDA Jan-2026 alignment | **Ready — the strongest area** |
| Code quality | ruff + pyright clean, 1,235 tests green, 0 TODOs | **Ready** |
| API surface | 22 routes declared and validated; **4 return 501** | **Not ready** |
| Persistence | Phase 1 only; six subsystems in-memory | **Not ready — blocking** |
| Audit & compliance evidence | Disclosure audit does not survive a restart | **Not ready — blocking** |
| Horizontal scalability | 3 replicas over process-local state | **Not ready — blocking** |
| Deployment & release | Builds and scans; never publishes or deploys | **Not ready** |
| Observability | Designed completely; connected to nothing | **Not ready** |
| Resilience | Retry/idempotency/DLQ strong; no breaker, no outbox | **Partial** |
| Performance | No measurement against real infrastructure | **Unknown — not "good" or "bad"** |
| DR / HA / multi-region | Documented; never exercised; Redis single replica | **Not ready** |
| Security | RLS proven (this session); rate limit per-replica; one unmaintained dep | **Partial** |
| Frontend | None | **Out of scope, by agreement** |

**Overall: not deployable.** Four blockers, and B-1 alone would cause a privacy incident rather
than an outage.

---

## 6. Phase 9 roadmap

Ten workstreams. Each is independently deliverable and separately committable.

---

### **W0 — Unify the application and complete the backend factories**

| | |
|---|---|
| **Objective** | One application: the Phase 8 container gains the Phase 1 database managers, and every configured backend has a factory that can build it. |
| **Business value** | Nothing ships until this exists. It is the precondition for every other claim. |
| **Engineering value** | Removes the two-application split permanently; makes "configured" and "buildable" the same statement. |
| **Affected services** | gateway, ingestion |
| **Affected packages** | `cip_gateway`, `cip_core.db`, `cip_platform.{cache,tasks,events}` |
| **Dependencies** | None — **this is the entry point** |
| **Complexity** | **Medium** — the pieces exist, in the wrong places |
| **Order** | **1** |
| **Acceptance** | Postgres/Mongo/Neo4j managers registered as container services with lifecycle + health probes · cache/queue/event factories select real implementations from settings · `RedisCache` reachable · Celery+Kafka implemented or removed from config · startup validation asserts every configured backend is **satisfiable and reachable**, not merely coherent · no route returns 501 · registry dead-route check also verifies an adapter exists |
| **Validation** | Startup validation in production mode against live containers; `test_ci_environment_variables_are_actually_read` extended to backends |
| **Rollback** | Feature-flag the unified app; the Phase 1 app remains bootable until parity is proven |
| **Production risks** | Lifecycle ordering — a manager closed before its dependents drain. Mitigated by the container's existing reverse-order shutdown. |

---

### **W1 — Persistence for Phases 2–7**

| | |
|---|---|
| **Objective** | Every in-memory store gains a durable implementation behind its existing protocol. |
| **Business value** | The platform survives a restart and can run more than one replica. Resolves B-1. |
| **Engineering value** | Turns six protocols from single-implementation into genuine ports. |
| **Affected services** | retrieval, interop, analytics, copilot |
| **Affected packages** | `cip_retrieval.{vectorstore,graph}`, `cip_interop.{fhir,empi,consent,streaming}`, `cip_analytics.warehouse`, `cip_core.db` |
| **Dependencies** | **W0** |
| **Complexity** | **High — the largest piece in Phase 9** |
| **Order** | **2** |
| **Acceptance** | Vectors → Atlas · graph → Neo4j · FHIR/EMPI/consent → PostgreSQL · warehouse → PostgreSQL (separate schema) · events → decided backend · **every new table carries `tenant_id` and an RLS policy in its first migration** · in-memory implementations retained as test doubles · restart loses nothing · two replicas serve identical data |
| **Validation** | Isolation test per backend, **written first** · restart test · two-replica consistency test |
| **Rollback** | Per-store settings switch back to in-memory; migrations are additive and reversible |
| **Production risks** | **Tenant isolation implemented differently per backend.** Mongo and Neo4j have no RLS equivalent — isolation must be enforced in the query layer and proven, not assumed. This is the highest-severity risk in Phase 9. |

---

### **W2 — Durable, tamper-evident audit trail**

| | |
|---|---|
| **Objective** | A `PostgresAuditSink` behind the existing protocol; append-only; hash-chained. |
| **Business value** | Resolves B-2. Without it there is no HIPAA §164.312(b) evidence and no defensible break-glass record. |
| **Engineering value** | Joins the two audit systems that were never connected. |
| **Affected services** | interop, gateway | **Affected packages** | `cip_interop.consent`, `cip_core.models` |
| **Dependencies** | W0 (connection), W1 (schema conventions) |
| **Complexity** | **Medium** |
| **Order** | **3** |
| **Acceptance** | Append-only table, no UPDATE/DELETE grant to the app role · rows hash-chained so tampering is detectable · break-glass replicated to longer retention · **startup refuses production with an in-memory audit sink** (same shape as the de-identification salt check) |
| **Validation** | Tamper test — modify a row, prove the chain breaks · restart test · privilege test proving the app role cannot delete |
| **Rollback** | Sink is settings-selected; reverting is a config change |
| **Production risks** | Audit write latency on the disclosure path. Mitigate with async batching that **fails closed** — a disclosure whose audit write failed must not return data. |

---

### **W3 — Publish, sign, and deploy**

| | |
|---|---|
| **Objective** | A tagged commit produces a signed, attested image in GHCR and reaches a staging environment. |
| **Business value** | Resolves H-2. Until this exists there is no path from commit to running system. |
| **Engineering value** | Makes the nine manifests executable rather than aspirational. |
| **Affected packages** | `.github/workflows`, `deploy/k8s`, `docker` |
| **Dependencies** | None (parallel with W1) |
| **Complexity** | **Medium** |
| **Order** | **4 (parallel from the start)** |
| **Acceptance** | Tag trigger → build → cosign keyless sign → push to GHCR · **SBOM and provenance retained** (currently generated and discarded) · manifests reference the real image · **`integration` added to `build.needs`** (H-11) · **third-party actions pinned to a SHA**, not `@master` · base image pinned by digest (M-16) · migration Job manifest (M-12) · `kubectl apply --dry-run=server` against a real cluster in CI · staging deploy reaching a live readiness probe |
| **Validation** | `cosign verify` in CI; staging smoke test |
| **Rollback** | Deployment is `RollingUpdate`; previous image tag remains signed and pullable |
| **Production risks** | Registry credentials in CI. Use OIDC keyless — no long-lived secret. |

---

### **W4 — Connect observability**

| | |
|---|---|
| **Objective** | Metrics scraped, traces exported, every alert backed by a metric that exists. |
| **Business value** | Nine runbooks become actionable. Resolves H-1. |
| **Engineering value** | The Phase 8 correlation id becomes a distributed trace across all ten stages. |
| **Affected packages** | `cip_platform.observability`, `cip_gateway`, `observability/`, `deploy/k8s` |
| **Dependencies** | W0 |
| **Complexity** | **Medium** |
| **Order** | **5** |
| **Acceptance** | `/metrics` endpoint, declared in the registry with a `public_reason`, bound to the pod network · `ServiceMonitor` added · **all 9 alert expressions verified against emitted metrics** · OTel wired to the configured OTLP endpoint · correlation id propagated as trace id · `trace_sample_ratio` honoured, errors tail-sampled · GenAI semconv **pinned**, attribute construction isolated in one module |
| **Validation** | Query every alert expression against a live Prometheus and assert non-empty; one request producing a 10-span trace |
| **Rollback** | Telemetry is settings-gated; disable without redeploying |
| **Production risks** | Metric cardinality. The existing cardinality guard in `metrics.py` must cover the new instruments. |

---

### **W5 — Shared rate limiting and caching**

| | |
|---|---|
| **Objective** | Limits and caches become properties of the tenant, not of the replica. |
| **Business value** | Resolves H-3 — a real limit under horizontal scale. Also fixes M-3 (embedding cache off). |
| **Affected packages** | `cip_platform.{security.limits,cache}`, `cip_gateway.platform` |
| **Dependencies** | W0 (Redis factory) |
| **Complexity** | **Low–Medium** |
| **Order** | **6** |
| **Acceptance** | Redis-backed token bucket · limit holds across 3 replicas under test · embedding cache wired in the composition root · **`volumeClaimTemplates` replacing `emptyDir` on the Redis StatefulSet** · **NetworkPolicy granting `cip-redis` ingress from api/worker/scheduler** (H-8 — without both, W5 fails on first deploy) · Redis HA (M-6) or documented single-point acceptance |
| **Validation** | Three-replica load test proving aggregate limit is 600/min, not 1800 |
| **Rollback** | Settings switch to in-process limiter |
| **Production risks** | Redis becomes a hard dependency on the request path — needs the circuit breaker from W0. |

---

### **W6 — Real-infrastructure integration testing**

| | |
|---|---|
| **Objective** | Every backend exercised in CI, with a tenant-isolation test per backend. |
| **Business value** | Resolves H-4. Converts isolation from a documented claim to a verified one. |
| **Affected packages** | `tests/integration`, `.github/workflows` |
| **Dependencies** | Runs *ahead of* each W1 backend (tests first) |
| **Complexity** | **Medium** |
| **Order** | **Interleaved with W1 — precedes each backend** |
| **Acceptance** | MongoDB added to the service matrix · Redis and Neo4j actually contacted · **isolation test per backend** · all integration tests run as a non-superuser · fix M-2's two self-neutering tests · restore coverage for the three unmeasured packages and set a `fail_under` floor |
| **Validation** | Deliberately break isolation in each backend and confirm the test fails |
| **Rollback** | n/a — additive |
| **Production risks** | CI duration. Mitigate by running the full matrix on `main` and a subset on PRs. |

---

### **W7 — Resilience: circuit breakers and transactional outbox**

| | |
|---|---|
| **Objective** | Bound failure propagation; make store-plus-publish atomic. |
| **Business value** | Prevents a single slow dependency taking the platform down; prevents silent divergence between store and event stream. |
| **Affected packages** | `cip_platform`, `cip_interop.streaming`, `cip_core.db` |
| **Dependencies** | **Decided before W1; delivered with W0** |
| **Complexity** | **Medium** |
| **Order** | **1.5 — before persistence lands** |
| **Acceptance** | Breaker on every remote dependency, half-open probing, metrics · outbox table + relay so a resource write and its event are one transaction · dead-letter path reused |
| **Validation** | Fault injection — kill each dependency mid-load, assert the breaker opens and the platform degrades rather than stalls |
| **Rollback** | Breaker settings-tunable; can be set to never open |
| **Production risks** | A breaker tuned too tight causes self-inflicted outages. Start permissive, tighten on measurement. |

---

### **W8 — Performance validation and cost baseline**

| | |
|---|---|
| **Objective** | Replace Phase 0 estimates in `docs/nfr.md` with measurements against real infrastructure. |
| **Business value** | Capacity planning and a defensible cost model. |
| **Affected packages** | `scripts/`, `docs/nfr.md`, `docs/operations/sla-dr.md` |
| **Dependencies** | W1, W5 |
| **Complexity** | **Medium** |
| **Order** | **7** |
| **Acceptance** | Ingestion throughput, retrieval p95, warehouse query latency measured against real stores · **benchmarks gain thresholds and run in CI** (M-8) · published numbers carry their conditions · cost per tenant tier from measured resource use |
| **Validation** | Load test at documented NFR targets; regression thresholds fail the build |
| **Rollback** | n/a — measurement only |
| **Production risks** | Load testing against shared infrastructure. Use an isolated environment. |

---

### **W9 — Disaster recovery, exercised**

| | |
|---|---|
| **Objective** | Execute a restore and record the achieved RPO/RTO. |
| **Business value** | Resolves M-10 — turns a document into a capability. |
| **Affected packages** | `docs/operations/sla-dr.md`, `deploy/` |
| **Dependencies** | W1, W3 |
| **Complexity** | **Medium** |
| **Order** | **8** |
| **Acceptance** | Backup configured per store · restore into a clean environment executed · **achieved** RPO/RTO recorded against targets · cross-region failover runbook walked or explicitly deferred with reasoning |
| **Validation** | The drill itself, with timings |
| **Rollback** | n/a |
| **Production risks** | Restore testing against production backups. Use a copy. |

---

## 7. Recommended implementation sequence

```
W0  unify + factories + breakers ──────────────────────────────┐
      │                                                         │
      ├─▶ W7  outbox (decided before any write path exists)     │
      │                                                         │
      ├─▶ W6/W1 per backend, TEST FIRST:                        │
      │      isolation test → persistence → prove it fails      │
      │      ① consent + audit (W2)   compliance blast radius   │
      │      ② FHIR + EMPI            clinical correctness      │
      │      ③ vectors (Atlas)        rebuildable               │
      │      ④ graph (Neo4j)          rebuildable               │
      │      ⑤ warehouse              rebuildable from source   │
      │      ⑥ event stream           decision needed first     │
      │                                                         │
      └─▶ W3 publish/deploy (parallel from day one) ────────────┤
                                                                │
          W5 shared limits ──▶ W4 observability ──▶ W8 perf ──▶ W9 DR
```

**Two sequencing rules that carry the phase:**

1. **The isolation test for a backend is written before that backend's persistence.** It must fail
   first, and pass for the right reason. This is the direct lesson of the RLS finding — a test
   written after the code encodes the code's assumptions rather than the requirement's, and a test
   that never runs proves nothing at all.

2. **Order the stores by blast radius, not by difficulty.** Consent and audit first: they carry
   compliance consequences and are the smallest. The warehouse last: it is rebuildable from source
   data, so a mistake there costs a reload rather than a disclosure.

---

## 8. Open questions requiring your decision

| # | Question | My recommendation |
|---|---|---|
| **1** | **Event stream: Kafka or Redis Streams?** Phase 6's partitioning design and ADR-0026's ordering guarantees assume Kafka semantics. Redis Streams is one fewer system and Redis is already required by W5. | **Kafka.** The ordering guarantee is load-bearing for clinical event replay, and inheriting it by accident from a Redis choice made for convenience is how a correctness property gets lost. |
| **2** | **Warehouse: same PostgreSQL instance or separate?** | **Separate instance, same cluster.** Protects the operational store from analytical load without a second operational surface. |
| **3** | **How real is "real infrastructure"?** Managed services (RDS/Atlas/Aura/ElastiCache) vs containers. | **Containers in CI, one managed staging environment.** Containers are free and prove the code; managed services prove the production path. |
| **4** | **Do we ship without a real model?** Embeddings, reranking, and the language model are deterministic baselines, and **no model SDK exists anywhere in the repository.** All three are honestly documented as baselines with clean seams. | **Yes — keep it out of Phase 9, and state it prominently in the README.** Model integration needs its own evaluation harness and is the whole of Phase 10. But "Clinical Intelligence Platform" with no model must be an *explicit, agreed* gap rather than a quiet one. |
| **5** | **Is a staging cluster available?** W3's acceptance criterion needs somewhere to deploy. | If none, W3 narrows to "signed image in GHCR + `--dry-run=server` against a kind cluster in CI" — and that limit should be **stated now**, not discovered at the acceptance gate. |

---

## 9. What Phase 9 does not do

Stated so the boundary is agreed rather than assumed:

- **No model integration** (Open Question 4) — Phase 10
- **No frontend** — dashboards remain server-side structures
- **No GraphRAG global search** (M-11) — a capability, not a readiness gap
- **No HITRUST/SOC 2 audit engagement** — the roadmap's original Phase 8 item, which needs a
  deployed system first
- **No refactoring of working Phase 1–8 code.** Every change is additive: new implementations
  behind existing protocols, new factories, new tests. The 1,235 passing tests stay passing.

---

**Sources for §1:** [OpenTelemetry GenAI conventions state, July 2026](https://john-hodge.com/blog/opentelemetry-genai-semantic-conventions/) · [OpenTelemetry GenAI observability](https://opentelemetry.io/blog/2026/genai-observability/) · [FDA revised CDS guidance, January 2026 — Covington](https://www.cov.com/news-and-insights/insights/2026/01/5-key-takeaways-from-fdas-revised-clinical-decision-support-cds-software-guidance) · [FDA CDS software guidance](https://www.fda.gov/regulatory-information/search-fda-guidance-documents/clinical-decision-support-software) · [Arnold & Porter analysis](https://www.arnoldporter.com/en/perspectives/advisories/2026/01/fda-cuts-red-tape-on-clinical-decision-support-software)
