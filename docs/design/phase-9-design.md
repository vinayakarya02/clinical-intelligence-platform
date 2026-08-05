# Phase 9 Design — Real Infrastructure & Operational Readiness

**Status:** Proposed — awaiting review. No code has been written.

**Premise:** Phases 1–8 built a platform that is architecturally complete and operationally
unreal. Phase 8 proved the nine services compose, start, serve, and enforce authorisation. It
also proved that nothing had ever run outside one developer's machine — and publishing the
repository proved the point again, immediately.

This phase is about the gap between *correct* and *deployable*.

---

## 1. What publication itself found

Pushing to GitHub ran the pipeline for the first time in the project's history. Seven jobs, and
the failures were not flaky — every one was a real defect that only a machine without the
developer's working tree could surface:

| Finding | Why it survived eight phases |
|---|---|
| `.gitignore` excluded `cip_core/storage/` — the object-storage abstraction was **never committed** | files on disk, imports resolved, 1,232 tests passed |
| `aiosqlite` undeclared | present in the venv by accident |
| CI set `CIP_POSTGRES_DSN` / `CIP_NEO4J_URI`, which **no settings field reads** | nested settings need `__`; nothing checked the names |
| **Row-level security was never actually enforced** in any test run | the CI role was a superuser, and superusers bypass RLS unconditionally |
| `pip-audit` had never reported a real vulnerability | it failed on the project's own package first |
| The Docker image had never been built or scanned | no daemon locally |

The RLS finding is the one to sit with. Tenant isolation is the platform's foundational security
control. It was designed in Phase 0, implemented in Phase 1, documented across three architecture
documents — and the test proving it had never once executed against a database where it could
possibly fail. It passes now, on a non-superuser role, and that is the first evidence the
guarantee holds.

**The generalisation, and the thesis of Phase 9:** every control this platform claims is worth
exactly as much as the last time something independent tried to break it. Six phases of green
suites established very little about production behaviour.

---

## 2. Research findings — the current state, with evidence

### 2.0 There are two applications, and only one of them has a database

This is the finding that reorders the rest.

`libs/cip_core/db/` contains **real, complete** managers: `PostgresManager` with RLS-scoped
`tenant_session`, `MongoManager` on motor, `Neo4jManager`. They work. They are constructed in
exactly one place — `services/ingestion/src/cip_ingestion/api/dependencies.py:65-67` — which
belongs to the **Phase 1 ingestion app**, a separate FastAPI application.

Phase 8's composition root (`services/gateway/src/cip_gateway/platform.py`) constructs **zero**
persistence backends. Every one of its nine services is in-memory.

So the platform is currently two applications:

| | Phase 1 ingestion app | Phase 8 unified app |
|---|---|---|
| Real PostgreSQL / Mongo / Neo4j | **yes** | no |
| Unified HTTP surface, DI container, startup validation | no | **yes** |
| Serves FHIR, analytics, decision, copilot | no | yes (in-memory) |

Neither is deployable alone. Phase 8 integrated the *services* and left the *storage layer*
behind in Phase 1, which is precisely the kind of gap that a working demo hides — the demo runs
the unified app, and the unified app never needed a database because nothing it holds survives
the process.

**Phase 9's first job is therefore not "add persistence". It is to join these two applications**,
and only then to extend the storage layer to the services that lack one.

### 2.0.1 Related: four declared routes return 501

`services/gateway/src/cip_gateway/app.py:267` returns
`501 not implemented` for any service without an HTTP adapter. Only `interop` and `analytics`
have one. The route registry declares `/v1/search`, `/v1/ask`, `/v1/documents`, and
`/v1/documents/{id}` — the ingestion routes are served by the *other* application, and the
retrieval routes are served by neither.

Phase 8's registry validator does not catch this: its "dead route" check asks whether the backing
service is *registered*, and `retrieval` is. It does not ask whether an adapter exists. That is a
real hole in a Phase 8 control, and closing it belongs here.

### 2.1 The platform's data has nowhere to live

| Layer | Backing store | Evidence |
|---|---|---|
| Documents, chunks, tenants, audit, runs | **PostgreSQL** — real | `migrations/versions/0001_phase1_document_intelligence.py`, 7 tables |
| Vectors | in-process dict | `InMemoryVectorStore` in `platform.py` |
| Knowledge graph | in-process dict | `InMemoryGraphStore` |
| FHIR resources | in-process dict | `RepositoryRegistry()` |
| EMPI person index | in-process dict | `EmpiIndex()` |
| Consent records | in-process dict | `ConsentEngine` |
| Event stream | in-process list | `EventStream(partitions=8)` |
| Analytics warehouse | in-process dict | `Warehouse(schema)` |

**One migration exists. It creates seven tables, all from Phase 1.** Phases 2 through 7 — six
phases, roughly 30,000 lines — added no persistence whatsoever. `grep -l __tablename__` over
`services/interop` and `services/analytics` returns nothing.

The consequence is not "some features are slow". It is that a pod restart loses every patient
identity link, every consent record, every FHIR resource, and the entire warehouse. A second
replica shares none of it. The platform cannot be deployed with `replicas: 2`, and
`deploy/k8s/10-api-deployment.yaml` requests exactly that.

### 2.2 The disclosure audit trail does not persist

`AuditSink` is a Protocol with **one** implementation:

```
services/interop/src/cip_interop/consent.py:268
class InMemoryAuditSink:
    """An audit sink for tests and single-process runs."""
```

Every consent decision, every purpose-of-use evaluation, and every break-glass access is written
to a bounded in-process buffer. The buffer drops oldest-first (break-glass exempted, which was a
good Phase 6 decision) and disappears entirely on restart.

Phase 1 has a real `audit_log` table with a repository — a *different* audit system, serving
ingestion. The two were never joined.

For a platform whose security documentation commits to HIPAA §164.312(b) audit controls, **the
audit trail is the one thing that must outlive the process**, and it is the one thing that
doesn't. This is a compliance blocker, not a hardening item.

### 2.3 There is no way to ship the image

`.github/workflows/ci.yml` builds the image and scans it. `push: false`. There is no registry,
no tag, no signature, no attestation retained, no environment, no deployment. `grep -rn "push:
true\|ghcr.io\|kubectl apply\|helm" .github/workflows/` returns nothing.

Nine Kubernetes manifests exist and have never been applied to a cluster. The image tag they
reference — `ghcr.io/example/cip:0.4.0` — points at a registry path that does not exist.

### 2.4 Observability is fully designed and not connected

This one is unusually crisp:

- `MetricRegistry.render()` emits Prometheus text format (`metrics.py:175`)
- `observability/prometheus/alerts.yml` defines **9 alerts**
- `observability/grafana/cip-overview.json` is a complete dashboard
- `docs/operations/runbooks/` holds **9 runbooks**, one per alert
- **No HTTP endpoint exposes the metrics.** The route registry has no `/metrics`.

So Prometheus is configured to scrape an endpoint that does not exist, to fire alerts on metrics
nothing publishes, into runbooks nobody can action.

Distributed tracing is worse: `TelemetryPolicy` has `tracing_enabled`, `trace_sample_ratio`, and
`otlp_endpoint`; the ConfigMap sets an OTLP collector address. `grep -rn "opentelemetry"` over
`libs/` and `services/` returns **nothing**, and OpenTelemetry is not a dependency. Phase 8
threaded a correlation id through all ten pipeline stages; it reaches logs and no trace backend.

### 2.5 Real-infrastructure testing is one backend deep

1,247 tests. **10** touch real infrastructure, all in
`tests/integration/test_postgres_integration.py`. CI starts PostgreSQL, Redis, **and Neo4j**
services — Redis and Neo4j are never contacted by a single test.

MongoDB has no service at all, so the Atlas vector-search path — the production vector store per
ADR-0009 — has never executed.

### 2.5.1 Production configuration demands components that have no implementation

`PlatformSettings` refuses `cache.backend == "memory"` and `queue.backend == "memory"` in a
deployed environment — correctly. The ConfigMap therefore sets `redis`, `celery`, and `kafka`.

- `RedisCache` exists and is real (`libs/cip_platform/cache/redis_cache.py:38`) and is
  **instantiated nowhere** in `libs/` or `services/`. No factory chooses between it and
  `InMemoryCache`.
- Celery and Kafka backends **do not exist at all**. `libs/cip_platform/tasks/` and `events/`
  contain `base.py` and `memory.py` and nothing else.

So production configuration names three backends: one implemented but unreachable, two absent.
Phase 8's startup validation passes because it checks that the *settings* are coherent — nothing
asks whether a component exists to satisfy them. A real production start would either use the
in-memory implementation regardless or fail at first use, depending on the path.

This is the same class as the Phase 8 ConfigMap finding, one level up: configuration that
describes a system nobody built.

### 2.5.2 The "intelligence" is entirely deterministic baselines

Worth stating plainly, because the repository's name makes a claim:

| Component | Implementation | Real alternative |
|---|---|---|
| Embeddings | `HashingEmbeddingProvider` — SHA-1 tokens, char n-grams, L2-normalised | none in the repo |
| Reranking | `FeatureReranker` — linear scorer over pipeline signals | cross-encoder, "needs a model this phase does not have" |
| Keyword retrieval | genuine in-process Okapi BM25 | OpenSearch, named and not implemented |
| Language model | `ExtractiveLanguageModel` — selection and templating, not generation | none |

A repository-wide search for `anthropic|openai|bedrock|sentence-transformer|huggingface|cohere`
across `libs/` and `services/` returns **zero hits**: no SDK, no client, no adapter, no
configuration surface for one.

Every one of these is *honestly documented as a baseline* in its own docstring — Phase 2 and
Phase 3 both said so in their reports, and the seams (`EmbeddingProvider`, `Reranker`,
`LanguageModel` protocols) exist precisely so a real model can be substituted. That is good
design and it is not the same thing as having done it.

**Phase 9 does not fix this**, and that is a deliberate scoping decision worth surfacing at
review: model integration is a large, separate piece of work with its own evaluation
requirements, and doing it before the platform can persist a consent record would be the wrong
order. But "Clinical Intelligence Platform" and "no model" should be an explicit, agreed gap
rather than a quiet one. See Open Questions.

### 2.6 Security posture

| Item | State |
|---|---|
| Tenant isolation (RLS) | **now proven**, on a non-superuser role, as of this session |
| Rate limiting | in-process token bucket → the configured 600/min per tenant is enforced **per replica**; three replicas mean 1800/min |
| `python-jose` | unmaintained since 2021, one import site, pulls `ecdsa` with an unfixable advisory (PYSEC-2026-1325, currently suppressed with justification) |
| Secrets | mounted correctly as of Phase 8; no rotation path, no external secret store wired |
| mTLS / service mesh | not present |

### 2.7 Performance and scale are unmeasured

Phase 0 produced scale-ceiling *estimates*. `docs/nfr.md` states latency and throughput budgets.
Nothing has been measured against real infrastructure — every benchmark in Phases 2–8 ran
in-process against in-memory stores, which measures Python, not the system.

---

## 3. Phase 9 objectives

**One sentence:** make every claim this platform makes verifiable against real infrastructure,
and make the platform survive a restart.

Explicitly **not** in scope: new clinical capability, new AI features, a web UI, HITRUST/SOC 2
audit engagement. Those are Phase 10+. Adding features to a platform that cannot persist a
consent record is the wrong order.

---

## 4. Workstreams

### P0 — Blocks any real deployment

#### W0. One application

Before anything is persisted, the two applications become one. The Phase 1 ingestion app holds
the working `PostgresManager` / `MongoManager` / `Neo4jManager` wiring; the Phase 8 composition
root holds the container, startup validation, route registry, and unified surface. Neither is
deployable alone.

- Register the three database managers as services in `platform.py`, with lifecycle (connect on
  start, close in reverse order) and health probes wired to the existing `HealthService`
- Add the cache, queue, and event-bus **factories that do not exist**: a settings-driven choice
  between the in-memory and real implementations, so `cache.backend = "redis"` builds the
  `RedisCache` that is already written and currently unreachable
- Implement or remove the Celery and Kafka backends the configuration already names. Naming a
  backend with no implementation is worse than naming none.
- Extend startup validation: for every configured backend, assert an implementation exists and
  is reachable. Phase 8 checks that settings are *coherent*; this checks they are *satisfiable*.
- Mount HTTP adapters for `retrieval` and `ingestion`, or remove their routes from the registry.
  Four declared routes currently return 501.
- Close the registry hole that let this happen: the dead-route check must ask whether an adapter
  exists, not merely whether the service is registered.

W0 is a prerequisite for W1 and is smaller than it looks — the pieces exist, in the wrong places.

#### W1. Persistence for Phases 2–7

The largest piece, and the one everything else waits on.

| Store | Target | Rationale |
|---|---|---|
| Consent records, EMPI links, FHIR resources | PostgreSQL | relational, transactional, already the operational store, already has RLS |
| Vectors | MongoDB Atlas vector search | ADR-0009 chose it; the abstraction exists and has never run |
| Knowledge graph | Neo4j | the driver and Cypher writer exist from Phase 2 |
| Warehouse facts and dimensions | PostgreSQL (separate schema) | star schema is relational by construction |
| Event stream | Redis Streams or Kafka | decision required — see Open Questions |

Design constraints:
- **Every new table carries `tenant_id` and an RLS policy from its first migration.** Retrofitting
  isolation is how it gets missed.
- The in-memory implementations stay, as the test double and the local-development path. They
  become one implementation of a port, not the only one.
- Migrations are additive and reversible. One migration per bounded context, not one giant one.

#### W2. Durable audit trail

A `PostgresAuditSink` implementing the existing `AuditSink` protocol, writing to an append-only
table with no `UPDATE` or `DELETE` grant for the application role.

- Break-glass entries replicated to a separate, longer-retention store
- Hash-chained rows so tampering is detectable — an audit log that can be edited silently is not
  evidence
- The startup validator refuses to start in production with an in-memory audit sink. This is the
  same shape as the de-identification salt check, and for the same reason: the default is not
  merely weak, it is a compliance failure that looks like working software.

#### W3. Publish and deploy path

- Build, sign (cosign, keyless OIDC), and push to GHCR on tag
- SBOM and provenance attestations **retained** — Phase 8's CI generates and discards them
  because that job never pushes
- Kubernetes manifests updated to the real image path; `kubectl apply --dry-run=server` against
  a real cluster in CI
- A staging environment that a deploy actually reaches, because "the manifests are valid YAML"
  is not the same claim as "the manifests run"

### P1 — Blocks operating it

#### W4. Close the observability loop

- A `/metrics` endpoint on the gateway, unauthenticated, bound to the pod network only —
  declared in the route registry with a `public_reason`, per Phase 8's rule
- Verify every one of the 9 alert expressions against metrics the platform actually emits. An
  alert on a metric nobody publishes is worse than no alert: it reads as coverage.
- OpenTelemetry instrumentation: add the dependency, wire the OTLP exporter to the already-
  configured endpoint, and propagate the Phase 8 correlation id as the trace id so a slow request
  can be followed across all ten pipeline stages
- Honour `trace_sample_ratio`; tail-sample errors regardless

#### W5. Cluster-wide rate limiting

Move the token bucket behind Redis so the limit is a property of the tenant rather than of the
replica count. Phase 8's startup validation already warns about this in production; the warning
becomes unnecessary once the limiter is shared.

#### W6. Real-infrastructure integration testing

- Add MongoDB to the CI service matrix; exercise the Atlas vector path
- Actually use the Redis and Neo4j services already running and untouched
- **A tenant-isolation test per backend.** RLS is proven for PostgreSQL; the equivalent guarantee
  in MongoDB, Neo4j, and Redis is currently asserted by architecture documents and by nothing
  else.
- Run the integration suite against a non-superuser role everywhere, matching production

### P2 — Hardening

#### W7. Replace `python-jose` with PyJWT

One import site (`services/ingestion/src/cip_ingestion/api/security.py`). Removes the `ecdsa`
dependency and the advisory suppression with it.

#### W8. Performance validation against real infrastructure

Replace Phase 0's estimates in `docs/nfr.md` with measurements: ingestion throughput, retrieval
p95, warehouse query latency, all against the real stores under W1. Publish the numbers and the
conditions.

#### W9. Backup, restore, and disaster recovery — exercised

`docs/operations/sla-dr.md` states RPO and RTO targets. Phase 9 restores a backup into a clean
environment and measures the actual numbers. A DR plan that has never been executed is a
document, not a capability.

---

## 5. Sequencing

```
W0 one application ──▶ W1 persistence ──┬──▶ W2 audit ──▶ W6 isolation tests ──▶ W8 performance
                                        │
                                        └──▶ W3 publish/deploy ──▶ W4 observability ──▶ W9 DR
                                                                      │
                                             W5 rate limiting ────────┘
                                             W7 PyJWT (independent, any time)
```

W0 gates everything: there is nowhere to put a database connection until the two applications are
one. W1 then gates the rest — there is no point measuring the performance of, or backing up, an
in-memory dictionary.

---

## 6. Acceptance criteria

Phase 9 is done when all of these hold, each demonstrated by something that runs:

0. One application starts, serving every declared route — **no route returns 501**, and every
   configured backend has an implementation that startup validation proved reachable
1. The platform restarts with **no data loss** — vectors, graph, consent, EMPI, FHIR, warehouse
2. Two replicas serve the same data and share one rate limit
3. Every consent decision and break-glass access is in an append-only store, and the platform
   **refuses to start in production** if the audit sink is in-memory
4. A tagged commit produces a signed image in GHCR with a retained SBOM, and a staging deploy
   reaching a live readiness probe
5. Prometheus scrapes a real `/metrics`; all 9 alerts reference metrics that exist; a trace for
   one request spans all ten pipeline stages in a trace backend
6. A tenant-isolation test passes **per backend**, on a non-superuser role
7. `docs/nfr.md` contains measured numbers with stated conditions, not estimates
8. A restore from backup is executed and the achieved RPO/RTO recorded

---

## 7. Open questions for review

1. **Event stream: Redis Streams or Kafka?** Kafka matches the Phase 6 partitioning design and
   ADR-0026's ordering guarantees; Redis Streams is one fewer system to operate and Redis is
   already required for W5. The Phase 6 design assumed Kafka semantics — this decision should be
   made deliberately rather than inherited.
2. **Warehouse: same PostgreSQL instance or separate?** Separate protects the operational store
   from analytical load; same reduces operational surface at this scale.
3. **How real is "real infrastructure"?** Managed cloud services (RDS, Atlas, Aura, ElastiCache)
   versus containers in CI. Managed services test the actual production path and cost money and
   credentials; containers are free and prove less. My recommendation: containers for CI, one
   managed environment for staging.
4. **Do we accept shipping without a real model?** The embedding provider, reranker, and
   language model are deterministic baselines, and there is no model SDK in the repository at
   all. The seams are clean and the docstrings are honest, so this is a scoping decision rather
   than a defect — but a platform called "Clinical Intelligence" that retrieves by hashed
   n-grams and answers by templating should carry that gap explicitly. My recommendation: keep it
   out of Phase 9, state it prominently in the README, and make model integration the whole of
   Phase 10, where it can have the evaluation harness it needs.
5. **Is a staging cluster available?** W3's acceptance criterion needs somewhere to deploy. If no
   cluster is available, W3 narrows to "signed image published + `kubectl --dry-run=server`
   against a kind cluster in CI", and that limit should be stated rather than discovered.

---

## 8. Risk

The largest risk is W1's size. Six phases of in-memory implementations become persistent ones,
and every one is a place where tenant isolation can be got wrong in a new way. The mitigation is
W6 ordering: **the isolation test for a backend is written before that backend's persistence is
wired**, so it fails first and passes for the right reason. That is exactly the discipline the
RLS finding argues for — the test that never runs proves nothing, and the test written after the
code tends to encode the code's assumptions rather than the requirement's.
