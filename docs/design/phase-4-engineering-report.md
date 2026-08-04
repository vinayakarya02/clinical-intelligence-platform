# Phase 4 Engineering Report — Production Platform

**Scope delivered:** production infrastructure around a functionally complete AI platform.
Docker, Kubernetes, a five-domain cache, an event spine, background workers, AI observability
on OpenTelemetry GenAI conventions, monitoring, security, MLOps registries, CI/CD,
environment-aware configuration, benchmarks, runbooks, and ADRs.

**Constraint honoured:** Phases 1–3 are consumed unchanged. Every capability here is additive
— a library the application calls and a composition root that wires it
([ADR-0013](adr-0013-platform-library-boundary.md)).

**Verification status:** 833 of 844 tests pass, 11 skip (integration tests needing live
PostgreSQL/MongoDB/Neo4j, and OCR needing Tesseract). `ruff format`, `ruff check`, and
`pyright` are clean. All 20 Kubernetes objects pass schema and policy validation.

---

## 1. Infrastructure architecture

`libs/cip_platform` is a **library, not a framework**. It exports protocols and
implementations; it starts nothing and wraps nothing. Cross-cutting behaviour is applied in
three places only:

- **Gateway middleware** — correlation, authn, authz, rate limit, budget, metrics. Every
  request passes through it, so nothing has to remember to call it.
- **Adapters** — `CachedEmbeddingProvider` satisfies Phase 2's existing `EmbeddingCache`
  protocol, so distributed caching is a constructor argument rather than a code change.
- **Observers** — `AnswerObserver` *reads* a finished Phase 3 `Answer` and emits telemetry.
  The copilot does not instrument itself and does not import the platform.

The dependency rule is one-way and enforced by a test: application code may import the
platform; the platform never imports application code.

| Module | Responsibility |
|---|---|
| `config` | Environment-aware settings that **refuse** unsafe deployed configurations |
| `correlation` | One id through logs, metrics, spans, events, and jobs |
| `cache/` | Five domains, one interface, a tenant in every key |
| `events/` | Typed spine; the bus emits `AuditLogged` itself |
| `tasks/` | Six job kinds, classified retries, dead-lettering |
| `observability/` | Metric registry with a cardinality guard; OTel GenAI conventions |
| `security/` | API keys, RBAC, token buckets, spend budgets, secret providers |
| `mlops/` | Model / embedding / evaluation registries and a compatibility matrix |
| `flags` | Off, on, or a tenant-stable percentage — nothing more |

## 2. Production topology

Three deployable roles from **one image**, selected by entrypoint
([ADR-0017](adr-0017-worker-topology.md)): `api` (3+ replicas, HPA to 20), `worker`
(2 replicas, queue consumers), `scheduler` (exactly 1, `Recreate` strategy).

One image because the evaluation job runs the copilot, the embedding job runs the retrieval
pipeline, and the ingest job runs the Phase 1 ETL — a worker with a genuinely minimal
dependency set could run none of them. One build, one scan, one SBOM, and version skew between
API and worker becomes impossible rather than merely unlikely.

20 Kubernetes objects. The choices worth defending:

- **Restricted Pod Security Standard enforced at admission**, plus a policy script that checks
  what a schema cannot: non-root, dropped capabilities, read-only rootfs, memory limits,
  probes, no floating tags, no mounted service-account token.
- **No CPU limit on the API.** A CPU limit throttles rather than evicts, and throttling a
  latency-sensitive service produces p99 spikes that read as an application bug. Memory *is*
  limited, because unbounded memory is an OOM for everything else on the node.
- **Default-deny NetworkPolicy first**, then explicit allows. Adding allows to a permissive
  baseline leaves whatever was not enumerated open.
- **`preStop` sleep before termination.** Without it kube-proxy still routes to a terminating
  pod — the classic source of 502s on every deploy.
- **Scheduler at one replica with `Recreate`.** A rolling update would briefly run two, which
  is the one thing that Deployment exists to prevent.
- **Redis `maxmemory` below the container limit**, so Redis evicts on its own policy before
  the kernel OOM-kills it and loses the whole cache instead of its coldest keys.

## 3. MLOps architecture

Four registries with one idea: **an artifact version is data, and promotion is reversible.**

- **Model registry** — stages, promotion with automatic demotion of the incumbent, and
  `rollback()` in one call. Registering directly into production is refused because it skips
  the bookkeeping rollback depends on.
- **Embedding version registry** — dimensions are a first-class property, because a dimension
  change is the one model change that makes an existing index unreadable rather than worse.
- **Prompt registry** — Phase 3's catalog, already versioned with pins and rollback.
- **Evaluation registry** — every run carries the model, prompt, and embedding versions that
  produced it. A score that does not say what produced it cannot be compared, which makes it
  decoration.

The **compatibility matrix** binds them. A deployment names three versions; the matrix says
whether that *combination* has been evaluated. Individually blessing each component says
nothing about the three together — an embedding change alters what is retrieved, which changes
what the prompt is given, which changes what the model answers. An un-evaluated combination is
refusable at startup rather than discoverable in production, and a **missing metric fails
rather than passes**, because treating absence as success is how an evaluation gate stops
gating.

## 4. Monitoring architecture

**OpenTelemetry GenAI semantic conventions, not a bespoke vocabulary**
([ADR-0016](adr-0016-otel-genai-conventions.md)). `gen_ai.usage.input_tokens`,
`gen_ai.tool.name`, `gen_ai.evaluation.score.value` — the standard names, so telemetry is
legible to Grafana, Datadog, or a vendor LLM-observability product without a translation
layer. Where no standard name exists (per-request USD cost, per-stage durations, cache hit
rates) a `cip.*` local extension is used and enumerated in `LOCAL_EXTENSIONS` so the
non-standard surface is auditable in one place.

Quality scores go out as `gen_ai.evaluation.*` rather than bespoke gauges, which is exactly
what those attributes are for. Phase 3 already produces confidence, hallucination rate, and
abstention, so observation is a projection rather than an instrumentation campaign.

The metric registry is hand-written rather than `prometheus_client` for one reason:
**cardinality is the failure mode that takes down a monitoring stack**, and owning the registry
makes the per-metric cardinality guard enforceable rather than advisory. A patient id or a
correlation id as a label is refused loudly instead of accepted quietly.

9 alerting rules, 12 dashboard panels, 9 runbooks — one per alert. Every rule alerts on a
*symptom a user would notice*, never on a cause: "answers are slow", not "CPU is high". Three
of the nine are AI-specific (hallucination rate, abstention rate, confidence collapse) and are
invisible to every infrastructure metric.

## 5. Security architecture

Defence in depth, each layer assuming the one outside it failed.

- **Authentication** — API keys hashed with a pepper and looked up by an indexed public
  prefix, compared in constant time. Every failure returns the identical message, because
  distinguishing "no such key" from "wrong secret" tells an attacker which half of a guess was
  right. `TokenVerifier` is the seam an OIDC verifier implements.
- **Authorisation** — roles → scopes in one table; routes declare the scope they need. A
  researcher has no `patients:read`, because a researcher who can read an identified record
  has defeated de-identification.
- **Rate limiting** — token buckets per tenant *and* per principal, the principal limit
  deliberately tighter so one leaked key cannot exhaust its tenant.
- **Cost governance** — a separate control, because a rate limit bounds *requests* and only a
  budget bounds *spend* ([ADR-0018](adr-0018-cost-governance.md)). ALERT then REJECT, with
  `Retry-After` pointing at the actual window boundary.
- **Tenant isolation** — the tenant comes from the credential, never from the request body.
  Cache keys cannot be constructed without one.
- **Secrets** — a provider protocol; files in Kubernetes (permissioned, not inherited by child
  processes like an environment variable) and never in a manifest. The shipped Secret is
  deliberately empty so a deployment that forgot to wire its real source fails readiness
  loudly.
- **Configuration refusal** — `PlatformSettings` refuses to start a deployed environment with
  an in-memory cache, an inline queue, a non-durable event bus, disabled metrics, or prompt-
  content recording. Each of those fails *silently* in production, which is the class of
  mistake worth refusing at startup.

## 6. Scalability assessment

Measured (§9) and reasoned:

**Scales horizontally today.** The API is stateless; every replica shares Redis, the queue,
and the datastores. The HPA scales up quickly and down slowly, because churn costs more than
idle replicas.

**Known ceilings, in the order they will be hit:**

1. **The event bus at ~10k publishes/s** in-process. Each publish emits an audit record, so
   the bus does two dispatches per call — deliberate, and the dominant cost. A Kafka backend
   moves this off the request path; the in-process bus is a development backend.
2. **Redis is a single instance.** Cache-only, so losing it is a slowdown rather than data
   loss, but it is a single point of *performance* failure. Redis Cluster changes the
   invalidation story: `SCAN` with `MATCH` only scans one node, so namespace sweeps would need
   fan-out.
3. **The scheduler is a single replica by design.** Correct, and it means periodic work stops
   during a node failure until it reschedules.
4. **Per-tenant partitioning means a single very large tenant cannot be split.** Ordering is
   per tenant, which no consumer here needs to exceed — but a tenant generating more events
   than one partition can absorb has no shard key to fall back on.
5. **The metric cardinality guard is per-process.** With a tenant label and a large fleet, the
   2,000-series budget is reached at roughly a few hundred tenants per instrument.

**Not addressed, deliberately:** service mesh, multi-region active-active, and autoscaling on
custom metrics. Each depends on traffic this system has never served, and guessing would
produce configuration that looks authoritative and has never been true.

## 7. Bugs found

Found by the adversarial pass and by the tests written alongside it.

### Blocker

**B1 — Roughly one API key in three was permanently unusable.** `secrets.token_urlsafe`
emits `_`, and `presented.split("_")` shattered the secret at its own underscores, so a
correctly-minted key failed authentication. The failure was **nondeterministic** — it depended
on the random bytes — so it would have presented as intermittent, unreproducible auth failures
affecting some customers permanently and others not at all. Found because a test minted a real
key rather than a hand-written one.

### High

**H1 — The rate limiter was itself a denial-of-service amplifier.** Eviction scanned every
bucket to find the oldest, so once the map was full each new key cost O(n). An attacker
rotating principal identifiers would force a 10,000-element scan *per request* — turning the
control that exists to prevent resource exhaustion into a way to cause it.

**H2 — Unbounded accumulation in the in-memory event bus and task queue.** `_published` grew
one entry per event forever, and those entries hold full payloads — so for PHI-carrying events
it retained clinical content for the life of the process. A memory leak and a retention
problem at once. The same applied to the task queue's results map, dedupe set, and
dead-letter list. Both components are the *development* backends, not only the test ones, so
"it's only a test helper" was not true.

### Medium (accepted, documented)

**M1 — `GatewayGuards.charge()` has no caller.** No route exists yet, so the budget never
accumulates and never trips. The control is correct and **unexercised**; it becomes live when
routes and a real provider are wired in. Stated here rather than presented as working.

**M2 — `CachedEmbeddingProvider` passes an empty `model_key`.** It works — Phase 2's cache key
already contains the model — but it reads as though the model were being ignored. A clearer
seam would take the pre-built key directly.

**M3 — The correlation ContextVar token is discarded.** Safe under asyncio, where each request
is its own task context; wrong if the gateway were ever driven from a thread pool.

## 8. Bugs fixed

B1, H1, and H2, each with a regression test:

- `split("_", 2)` plus a 50-key round-trip test — the original bug passed a single-key test
  roughly two times in three, so the test mints many.
- `OrderedDict` LRU for the limiter, with a test that fills the map then churns it and asserts
  the churn phase does not degrade.
- Bounded `deque` and `OrderedDict` for bus and queue history, with tests that publish 500
  events and enqueue 300 tasks and assert the bound — plus a test that deduplication still
  holds *within* the window, because bounding the dedupe set must not silently break
  idempotency.

M1–M3 are documented rather than fixed: M1 needs routes that do not exist yet, and M2/M3 are
clarity and hypothetical-context issues with no failure mode in the current deployment.

## 9. Benchmarks

In-process, Windows 11 / Python 3.11, no network.

| Operation | Throughput | Per operation |
|---|---|---|
| cache read (hit) | 106,944 /s | 9.35 µs |
| cache write | 73,203 /s | 13.66 µs |
| cache namespace sweep | 482,169 keys/s | 2.07 µs |
| rate limit check (warm) | 358,741 /s | 2.79 µs |
| rate limit check (full churn) | 81,446 /s | 12.28 µs |
| budget charge | 90,990 /s | 10.99 µs |
| API key authentication | 49,015 /s | 20.40 µs |
| counter increment | 146,956 /s | 6.80 µs |
| histogram observe | 51,990 /s | 19.23 µs |
| Prometheus render | 3,827 scrapes/s | 261 µs |
| event publish (incl. audit) | 9,735 /s | 102.72 µs |
| **copilot end-to-end** | **255 answers/s** | **3.92 ms** |

Copilot p50 / p95 / max: **3.8 / 5.2 / 5.9 ms**. Peak memory 67 MB.

**Platform overhead on the request path is ~33 µs against a ~3.9 ms answer — 0.83% of
end-to-end.** That number is the point of measuring: it is the evidence for the claim that
caching, authentication, rate limiting, and metrics can sit on every request without mattering.

The one figure that stands out is event publish at 102 µs, an order of magnitude slower than
its neighbours. That is the audit doubling — every publish emits a second event — and it is
the deliberate cost of making audit a property of the bus rather than of developer memory.

**All of these are in-process.** Redis, Kafka, a broker, and a model provider each add a
network round-trip that dominates every number above. Nothing here has run against real
infrastructure.

## 10. Test summary

**844 tests: 833 pass, 11 skip.** 107 are new in Phase 4, covering configuration refusal,
cache key scoping and invalidation, event audit emission and PHI summarisation, task retry
classification and dead-lettering, metric cardinality guards, API key verification, RBAC,
token buckets, spend budgets, secret providers, feature flags, the model registry and
compatibility matrix, correlation sanitisation, resource bounds, and manifest policy.

The load-bearing ones are the manifest policy tests (which run the same validator CI does),
the resource-bound regressions, and the API key round-trip that found B1.

## 11. Production readiness

**Ready:** the shape. Configuration refuses unsafe deployments at startup. Tenant isolation is
enforced at construction in the cache as it is in the vector store. Audit is emitted by the bus
rather than by convention. Telemetry uses a standard vocabulary. Alerts point at symptoms and
every one has a runbook. Manifests satisfy a policy that a schema cannot express, checked in
CI. Rollback is one call for both models and prompts.

**Not ready, in order of severity:**

1. **Nothing has run against real infrastructure.** No Redis, no Kafka, no Celery broker, no
   Kubernetes cluster. The Redis cache, the Kafka bus, and the Celery queue exist as
   implementations whose *semantics* are exercised by their in-memory counterparts, and whose
   *execution* has never happened. This is Phase 2's and Phase 3's gap, unchanged and now
   larger.
2. **The Docker image has never been built.** The Dockerfile is written and linted in CI, but
   no build has run here.
3. **No load test.** Every throughput number is single-process and sequential. Concurrency,
   connection pooling, and contention are unmeasured.
4. **The gateway has no routes.** `GatewayGuards` is exercised directly by tests; no HTTP
   surface mounts it, so the middleware ordering is verified by construction rather than by a
   request.
5. **The worker and scheduler entrypoints refuse to start.** Deliberately — a worker that
   boots without a broker, registers nothing, and reports healthy is the worst failure mode
   available — but it means neither has executed.
6. **Cost governance is unexercised** (M1), and will stay so until a metered provider exists.

**Assessment:** Phase 4 delivers production *architecture* with production *engineering* — the
refusals, the bounds, the guards, and the policy checks are real and tested. What it does not
deliver is production *validation*. Every component has been reasoned about carefully and
several have been improved by that reasoning; none has met a real cluster, a real broker, or
real concurrent load.

The honest framing is that the four phases have built a system whose *design* is defensible in
detail and whose *behaviour under real conditions* is still entirely unmeasured. The next unit
of work is not another layer; it is standing the thing up.

## 12. Technical debt

| Item | Why it matters | Next |
|---|---|---|
| No infrastructure validation | The largest gap in the whole project | Immediately |
| Redis Cluster invalidation | `SCAN`+`MATCH` scans one node; sweeps would need fan-out | Before sharding Redis |
| Gateway has no routes | Middleware order is verified structurally, not by a request | With the HTTP surface |
| Kafka/Celery backends unwritten | Only protocols and in-memory backends exist | With real infrastructure |
| Metric cardinality is per-process | 2,000 series ≈ a few hundred tenants per instrument | Before a large fleet |
| `CachedEmbeddingProvider` empty `model_key` | Reads as though the model were ignored | Cleanup |
| No blue-green or canary automation | Manifests support a rolling update only | With a deployment pipeline |
| Distributed tracing is configured, not emitted | OTLP endpoint is wired; no spans are created | With the HTTP surface |
