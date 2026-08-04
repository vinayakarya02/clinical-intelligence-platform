# Production Platform Architecture

**Status:** Phase 4 — implemented in [libs/cip_platform](../../libs/cip_platform/README.md) and [services/gateway](../../services/gateway/README.md)
**Depends on:** Phases 1–3, which are consumed unchanged
**Decisions:** [ADR-0013](../design/adr-0013-platform-library-boundary.md) … [ADR-0018](../design/adr-0018-cost-governance.md)

## 0. The constraint that shapes this phase

The AI platform is functionally complete and must not be redesigned. So every capability here
is **additive**: a shared library the application layers may call, and a composition root that
wires them together. Phases 1–3 are not edited to accommodate it.

That constraint is what produces the topology below. Instrumentation, caching, authorisation,
rate limiting, and cost governance all live at the **edges** — in middleware and in adapters —
rather than being threaded through the reasoning code. It is also better engineering: the
copilot has no opinion about Redis, and the cache has no opinion about clinical claims.

## 1. Runtime topology

```
                          ┌─────────────────────────────────┐
   clinician / system ───► │  Ingress (TLS, WAF, mTLS opt.)  │
                          └────────────────┬────────────────┘
                                           │
                          ┌────────────────▼────────────────┐
                          │  Gateway (API)  · 3+ replicas   │
                          │  correlation → authn → authz →  │
                          │  rate limit → budget → route    │
                          └───┬──────────────┬──────────┬───┘
                              │              │          │
              ┌───────────────▼──┐  ┌────────▼───────┐  │
              │ Copilot pipeline │  │ Retrieval      │  │
              │ (in-process)     │  │ (in-process)   │  │
              └───────┬──────────┘  └────────┬───────┘  │
                      │                      │          │
        ┌─────────────┼──────────────────────┼──────────┼─────────────┐
        │             │                      │          │             │
   ┌────▼────┐  ┌─────▼─────┐  ┌─────────────▼──┐  ┌────▼────┐  ┌─────▼─────┐
   │  Redis  │  │ Postgres  │  │ MongoDB Atlas  │  │  Neo4j  │  │   Kafka   │
   │ 5 cache │  │ operational│ │ vectors +      │  │  graph  │  │  events   │
   │ domains │  │ + audit    │ │ artifacts      │  │         │  │           │
   └─────────┘  └───────────┘  └────────────────┘  └─────────┘  └─────┬─────┘
                                                                       │
                          ┌────────────────────────────────────────────▼───┐
                          │ Worker (queue consumers) · Scheduler (periodic) │
                          │ ingest · embed · graph · evaluate · export      │
                          └─────────────────────────────────────────────────┘
                                           │
                          ┌────────────────▼────────────────┐
                          │ OTel Collector → Prometheus /   │
                          │ Grafana / trace backend         │
                          └─────────────────────────────────┘
```

Three deployable images — **api**, **worker**, **scheduler** — from one build. They share the
code and differ only in entrypoint, because a worker that cannot import the copilot cannot run
an evaluation job, and maintaining three dependency sets to avoid that is a cost with no
return at this size.

## 2. Caching

Five domains behind one interface, each with its own TTL and invalidation trigger:

| Domain | Keyed by | TTL | Invalidated by |
|---|---|---|---|
| embedding | model key + content hash | 30 d | model version change |
| retrieval | tenant + query hash + filters | 15 min | document ingest for the tenant |
| session | tenant + session id | 2 h | explicit end, or expiry |
| prompt | prompt name + version | 1 h | deployment change |
| graph | tenant + entity + hop budget | 1 h | graph write for the tenant |

**Every key carries its tenant, and the cache API cannot be called without one.** A cache is
the easiest place in a multi-tenant system to leak data, because a key collision is silent and
looks like a hit. `CacheKey` cannot be constructed without a tenant, mirroring `VectorQuery`
in Phase 2 ([ADR-0014](../design/adr-0014-cache-topology.md)).

Embedding cache entries are **content-addressed and tenant-scoped anyway**. That is deliberate
redundancy: embeddings of identical text are mathematically identical, so a shared cache would
be *correct* and would still mean one tenant's cache hit reveals that another tenant holds
that exact clinical text. The isolation is worth more than the hit rate.

## 3. Events

An append-only spine. Each event carries a tenant, a causation id, a correlation id, and the
W3C trace context, so a document's whole lifecycle is one trace:

```
DocumentUploaded → DocumentParsed → ChunkCreated → EmbeddingGenerated
                                                 → GraphUpdated
                                                 → EvaluationCompleted
                                                 → AuditLogged
```

`AuditLogged` is not a stage — it is emitted alongside every other event by the bus itself, so
"was this audited" is not a property of whether a handler remembered to call the audit log.

Consumers are idempotent by event id. At-least-once delivery is assumed because the
alternative is either lost events or distributed transactions, and a duplicate embedding write
is harmless where a lost one is not.

## 4. Observability

**OpenTelemetry GenAI semantic conventions**, not a bespoke vocabulary. The names below are the
standard ones (`gen_ai.*`), so any conformant backend — Grafana, Datadog, a vendor LLM
observability product — understands our telemetry without a translation layer. That is the
whole reason to prefer a standard here; the alternative is a dashboard that only works with
the dashboards we also wrote.

| Concern | Instrument |
|---|---|
| model latency | `gen_ai.client.operation.duration` histogram |
| tokens | `gen_ai.client.token.usage` histogram, `gen_ai.token.type` = input/output |
| cost | `gen_ai.client.cost.usd` — a local extension, flagged as such |
| quality | `gen_ai.evaluation.score.value` with `gen_ai.evaluation.name` |
| tools | span per call, `gen_ai.tool.name`, `gen_ai.tool.call.id` |
| retrieval | `gen_ai.operation.name` = `retrieval`, plus our stage histograms |

Phase 3's answers already carry exactly what these need — confidence breakdown, hallucination
rate, abstention mode, token usage, per-stage durations. The observability layer *reads* an
`Answer` and emits; it does not ask the copilot to instrument itself.

## 5. Security

Defence in depth, each layer assuming the one outside it failed:

1. **Ingress** — TLS, request size caps.
2. **Authentication** — API key (hashed at rest, prefix-indexed) or JWT. OAuth/OIDC-ready:
   the `TokenVerifier` protocol is what an OIDC verifier implements.
3. **Authorisation** — RBAC over roles → scopes, checked against the scope each route declares.
4. **Rate limiting** — token bucket per tenant *and* per principal, so one key cannot exhaust a
   tenant and one tenant cannot exhaust the cluster.
5. **Cost governance** — a spend budget per tenant with `ALERT` then `REJECT`
   ([ADR-0018](../design/adr-0018-cost-governance.md)). Rate limits bound *requests*; only a
   budget bounds *spend*, and for an LLM application those are different quantities.
6. **Tenant isolation** — already enforced in every store; the gateway re-derives tenant from
   the credential and never from the request body.
7. **Secrets** — a provider protocol; environment in development, mounted files or a KMS/Vault
   in production. Never in a manifest.
8. **Audit** — every authenticated request and every event, hash-chained (Phase 1).

## 6. MLOps

Four registries with one shared idea: **an artifact version is data, and promotion is a
transition that can be reversed.**

- **Model registry** — model versions with stages (`staging` → `production` → `archived`),
  metadata, and one-call rollback to the previous production version.
- **Embedding version registry** — which model key each vector index was built with. A change
  requires a re-index, so this is what makes that requirement visible rather than discovered.
- **Prompt registry** — Phase 3's catalog, already versioned with pins and rollback.
- **Evaluation registry** — scored runs, tied to the exact model, prompt, and embedding
  versions that produced them.

A **compatibility matrix** binds them. A deployment names a model version, a prompt version,
and an embedding version; the matrix says whether that combination has been evaluated. An
un-evaluated combination is refusable at startup rather than discoverable in production.

## 7. What is deliberately not here

No service mesh, no multi-region active-active, no autoscaling on custom metrics beyond CPU
and queue depth. Each is a real production concern and each is a decision that depends on
traffic this system has never seen. Guessing at them now would produce configuration that
looks authoritative and has never been true.
