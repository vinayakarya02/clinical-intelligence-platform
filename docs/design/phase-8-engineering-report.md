# Phase 8 Engineering Report — Enterprise System Integration

**Scope delivered:** a composition root with a dependency-injection container (topological
ordering, cycle detection, per-service criticality, reverse-order shutdown), a ten-stage
end-to-end clinical workflow with correlation-id propagation and cooperative cancellation, a
declared route registry with duplicate/dead/unreachable/shadowing detection, four-part startup
validation with fail-fast, a unified HTTP application that mounts Phase 6's and Phase 7's
previously unreachable APIs, mounted-secret loading, static deployment validation over 56
properties, and an in-process verification run.

**No new capability was added.** The objective was to make seven phases of existing work behave
as one production system.

**Verification:** 1,244 tests collected — 1,232 pass, 12 skip (integration tests needing live
PostgreSQL/MongoDB/Neo4j, OCR needing Tesseract). 50 are new in Phase 8. `ruff format`,
`ruff check`, and `pyright` clean. The full end-to-end run, the live HTTP surface, and the
deployment validation all execute in-process; what requires infrastructure is listed in §9 as
unverified rather than assumed.

---

## 1. What this phase found

Nine services, each with a passing suite and a completed adversarial review, had never been
started together. Doing so surfaced **eight construction errors in the first hour** and **five
deployment blockers** over the phase — none findable by any per-service test, because no
individual service was wrong.

### Construction errors (caught by the container, at startup)

| # | Error | Cause |
|---|---|---|
| 1 | `parents[4]` resolved to the repo root | path arithmetic off by one |
| 2 | `LexicalEmbeddingProvider` does not exist | renamed to `HashingEmbeddingProvider` |
| 3 | `provider.dimensions` / `InMemoryVectorStore(args)` | constructor signatures changed |
| 4 | `DocumentProcessor` missing `parsers` and `settings` | required arguments added later |
| 5 | `EmbeddingBatch.model_key` | moved to `batch.model.key` |
| 6 | `GraphNode(id=…, provenance=…)` | replaced by `label` / `key` |
| 7 | `LanguageModelInfo` called as a method | `model.info` is a property |
| 8 | `LanguageModelInfo.name` | renamed `model_name` |

Each is a two-line fix. The point is not the fixes; it is that eight interface assumptions had
drifted with nothing to detect it, and that a container which constructs everything at startup
found all eight in one run.

### Deployment blockers

| Severity | Finding | Consequence if shipped |
|---|---|---|
| **Blocker** | `docker/Dockerfile` copied 6 of 9 source packages | the image builds, starts, passes its health check, and cannot import decision, interop, or analytics |
| **Blocker** | `cip_core` wanted `prod`, `cip_platform` wanted `production`, both read `CIP_ENVIRONMENT`, every asset set the long form | every containerised start fails at settings load |
| **Blocker** | the ConfigMap configured only `cip_platform` | `cip_core`'s deployed-environment validation refuses the configuration; the pod cannot start |
| **Blocker** | the Secret mounted at `/var/run/secrets/cip` was read by nothing; three of its keys mapped to no settings field | every secret-derived setting holds its default in production — database password, Redis URL, broker URL, de-identification salt |
| **Blocker** | Phase 6's `ClinicalApi` (9 operations) and Phase 7's `AnalyticsApi` (8 operations) were mounted nowhere | two phases of work unreachable over HTTP |
| **High** | three FHIR operations shadowed by `/v1/fhir/{resource_type}` | `$export`, `$import`, and `$export/{id}` answer a plausible 404 instead of existing |
| **Medium** | `prom/prometheus:latest`, `grafana/grafana:latest`, no healthchecks | an observability stack that changes underneath you |

### Found by adversarial review of this phase's own code

| Severity | Finding | Consequence |
|---|---|---|
| **Blocker** | the HTTP adapter read the organisation from an `x-organization-id` header | the organisation is what consent is evaluated against — a caller could name any organisation and read across tenants |
| **Blocker** | the adapter read FHIR scopes from an `x-granted-scopes` header | a caller sends `system/*.read` and the consent layer honours it |
| **High** | query parameters passed positionally into `get_metric`, landing where the principal belongs | every authorisation decision in the analytics API made against a dict of untrusted query strings — caught by `pyright`, not by tests |
| **Medium** | `container.stop()` reported only services with stop hooks | a clean shutdown of ten services printed "0 stopped" |
| **Medium** | `→` in a rendered report | the startup report crashed on the Windows console codepage |

The two Blockers are the ones worth dwelling on. Both were in code written this phase, both
passed every test, and both were invisible for the same reason: **every test was sending the
headers it was supposed to send.** A test that only exercises the honest client cannot find a bug
that requires a dishonest one. The regression tests added for them assert the negative — that a
spoofed organisation changes nothing, and that a self-granted scope grants nothing.

---

## 2. Architecture

```
platform.py  ──registers──▶  ServiceContainer  ──builds──▶  9 services
     │                              │
     │                              ├── topological order, cycle detection
     │                              ├── lazy construction
     │                              ├── criticality: fail vs degrade
     │                              └── reverse-order shutdown
     │
     ├──▶ ClinicalPipeline    10 stages, 4 services, one correlation id
     ├──▶ RouteRegistry       22 routes, validated against the container
     ├──▶ startup validation  config, dependencies, routes, wiring
     └──▶ app.py              FastAPI, mounting in registry order
```

Three decisions carry the phase:

**One composition root.** `platform.py` and `container.py` are the only modules permitted to
import more than one service, enforced by a test that reads the source. Every service import
lives *inside* a factory, so a broken module takes down the service that needs it rather than
the process, and the failure reads as *this service failed to start* rather than as an
`ImportError` in somebody else's package. ([ADR-0037](adr-0037-composition-root.md))

**The HTTP surface is declared, not discovered.** A registry derived from the running app can
report what is there; it cannot report what should have been — and "should have been" is the
entire class of bug that left two phases unreachable.
([ADR-0038](adr-0038-declared-route-registry.md))

**Configuration is one contract.** The image, the ConfigMap, the Secret manifest, the loader map,
and the settings classes are checked against each other, because every configuration failure this
phase found lived in a correspondence that no single artefact owned.
([ADR-0039](adr-0039-configuration-is-one-contract.md))

---

## 3. The end-to-end workflow

Ten stages across four services, measured on a real discharge summary:

```
pipeline ok [corr-75466a339ef24157] in 6 ms
    parse            ingestion         1.8 ms   1 page
    metadata         ingestion         0.0 ms   type=discharge_summary
    chunk            ingestion         0.0 ms   5 chunks, quality 1.00
    embed            retrieval         1.3 ms   5 vectors, local/hashing-lexical-n4/384
    vector_store     retrieval         0.0 ms   5 records upserted
    knowledge_graph  knowledge_graph   0.0 ms   5 nodes
    retrieve         retrieval         0.6 ms   5 matches
    copilot          copilot           0.0 ms   extractive over 5 passages
    decision         decision          1.5 ms   6 rules evaluated
    analytics        analytics         0.2 ms   1 fact recorded
```

Per-stage timing rather than a total, so a slow run names the stage. Cancellation is polled
*between* stages — cooperative, and therefore unable to interrupt a stage mid-write. A stage
whose service is degraded is `SKIPPED`, not failed.

---

## 4. The HTTP surface

22 routes over 4 services, validated at startup: no duplicates, no dead routes, no unreachable
services, every anonymous route stating why it is anonymous, every route declaring its scope.

Live behaviour, exercised in-process against real minted credentials:

| Principal | Request | Result | Why |
|---|---|---|---|
| anonymous | `GET /v1/fhir/metadata` | 200 | conformance discovery discloses no patient data |
| anonymous | `GET /v1/analytics/metrics` | 401 | no credential |
| researcher | `GET /v1/analytics/metrics` | 200 | holds `analytics:read` |
| researcher | `GET /v1/fhir/Patient/p1` | 403 | no `patients:read` — a researcher who can read an identified record has defeated the de-identification |
| clinician | `GET /v1/fhir/Patient/p1` (no purpose) | 400 | purpose of use is never inferred |
| clinician | `GET /v1/analytics/metrics` | 403 | no `analytics:read` |

Both directions are asserted. A check that only tests the allowed direction passes when
authorisation is disabled entirely.

---

## 5. Startup validation

Ten checks in four groups, each moving a class of failure from *first request in production* to
*process start*, where a bad build fails its readiness gate and the previous version keeps
serving.

Production is strict where development is not. The de-identification salt is fatal in production
because its default is not merely weak but actively harmful: pseudonyms are
`HMAC(salt, identifier)`, so a salt committed to the repository lets anyone holding candidate
MRNs recompute every key in the warehouse and re-identify a dataset built specifically to be
de-identifiable.

Validation reports every failure rather than the first. An operator restarting on each successive
error learns one problem per crash loop.

---

## 6. Deployment validation

56 static checks over the Dockerfile, the compose file, nine Kubernetes manifests, and the CI
workflow. All pass.

The check that earns the module derives the expected package list **from the repository**, so a
service added in a later phase is covered the day it is added rather than the day someone
remembers. That check found the image shipping six of nine packages.

---

## 7. Tests

50 new tests, and the selection criterion is deliberate: **every one corresponds to a defect that
survived the phase that introduced it.** They are mostly cross-artefact — reading the Dockerfile,
the ConfigMap, and the Secret manifest and asserting they match what the code does — because a
test that only exercises Python cannot catch a deployment that ships the wrong files.

| Group | Count | Asserts |
|---|---|---|
| Deployment assets | 3 | package drift, no blocking findings, unverified ≠ passing |
| Configuration | 12 | both vocabularies, ConfigMap completeness, secret three-way correspondence, override precedence |
| Container | 5 | full start, cycle detection, degrade vs abort, reverse shutdown |
| Route registry | 9 | duplicate, dead, unreachable, shadowing, literal-before-template, declared scopes |
| Startup validation | 3 | development warns, production refuses a default salt, all failures reported |
| HTTP surface | 10 | mounting, anonymous discovery, deny-by-default, scope both ways, 503 on degraded |
| Trust boundary | 4 | no identity from client headers, scopes from the principal, spoofing changes nothing |
| Service boundaries | 4 | composition root only, no upward/sideways imports, one service list |

---

## 8. What changed in earlier phases

Six files, each a genuine production bug rather than a preference:

| File | Change |
|---|---|
| `docker/Dockerfile` | copy all nine source packages |
| `docker/docker-compose.yml` | pin Prometheus and Grafana; add healthchecks and a readiness-gated dependency |
| `deploy/k8s/01-configmap.yaml` | add every `cip_core` production requirement |
| `deploy/k8s/02-secrets.yaml` | reconcile keys to fields that exist |
| `libs/cip_core/config.py`, `libs/cip_platform/config.py` | accept both environment vocabularies |
| `services/gateway/.../container.py` | `stop()` reports every service torn down |

No service logic was modified. No working implementation was replaced.

---

## 9. What was not verified

Docker, Kubernetes, PostgreSQL, MongoDB, Neo4j, and Redis were unavailable. Everything that can
run in-process does; the rest is named rather than assumed:

`docker build` · `compose up` · `kubectl apply --dry-run=server` · image vulnerability scanning ·
PostgreSQL row-level security · MongoDB Atlas vector search · Neo4j traversal · cluster-wide rate
limiting.

The last is a real production gap rather than a testing inconvenience: the token-bucket limiter
is in-process, so the configured 600/min per tenant is enforced independently by every replica.
A limit that loosens with every scale-out is loosest exactly when it is most needed, and startup
validation warns about it in production.

---

## 10. The finding that generalises

Across Phases 5, 6, 7, and 8, the serious defects were found by **running the system**, never by
the tests written alongside the code. Phase 8 sharpens the observation into something more
specific:

> Every defect in this phase lived in a **correspondence** — between an image and a repository,
> between two settings vocabularies, between a mounted secret and the code that reads it, between
> a declared API and a mounted one, between a request header and the credential that should have
> been trusted instead.

A correspondence has no owner. It belongs to no module, appears in no service's test suite, and
is nobody's job until the day it fails. Unit tests cannot find these by construction, because
each side is individually correct.

The response is not more tests of the parts. It is to give each correspondence an artefact that
checks it, and to run that artefact at startup — so the disagreement fails a readiness gate
instead of a patient's request.
