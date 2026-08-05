# 12. System Integration

*Phase 8. How nine services become one platform.*

Phases 1 to 7 built capabilities. This phase built none. Its entire subject is the joins between
what already existed — which is where, it turns out, most of the remaining defects were.

---

## 1. The shape of the problem

At the end of Phase 7 the platform was nine services, 1182 passing tests, twelve architecture
documents, and thirty-six ADRs. Every service had been reviewed adversarially and every review's
findings had been fixed.

The nine services had never been started in the same process.

Doing so produced eight construction errors in the first hour and five deployment blockers over
the phase. Not one of them was findable by a test of any individual service, because no
individual service was wrong. The errors were in the assumptions each service's *caller* made:
a factory name that had changed, a property being called as a method, a constructor that had
grown a required argument, an image that did not ship the package, a settings class that did not
recognise the word its own ConfigMap used.

This is the characteristic failure mode of a system assembled in phases, and it does not
announce itself. Every gate stays green. The system simply does not work when run.

---

## 2. The composition root

```
                        ┌──────────────────┐
                        │  ServiceContainer│  topological sort, cycle detection,
                        │                  │  lazy construction, reverse shutdown
                        └────────┬─────────┘
                                 │ registers
                        ┌────────▼─────────┐
                        │    platform.py   │  the only module that knows every service
                        └────────┬─────────┘
                                 │
   ┌──────────┬──────────┬───────┴───┬──────────┬──────────┬──────────┐
settings   gateway     audit    ingestion  retrieval   decision   interop   analytics
   │          │          │          │          │          │          │          │
   └──────────┴──────────┴──────────┴─────┬────┴──────────┴──────────┴──────────┘
                                    knowledge_graph ── copilot
```

Each service is registered as a name, a factory, a dependency list, and a criticality flag.
Nothing is constructed until something asks for it; the container resolves dependencies
depth-first and detects cycles by tracking the resolution stack.

**Criticality decides what a failure means.** Analytics is `critical=False`: an empty warehouse
is a reporting gap, and refusing to serve clinicians over a reporting gap is the wrong trade. It
degrades, `try_get` returns `None`, and its routes answer 503 — not 500, because the request is
fine and the dependency is not, and that difference decides whether a client should retry.
Retrieval and copilot are critical; their failure aborts startup.

Rationale and alternatives: [ADR-0037](../design/adr-0037-composition-root.md).

### Layering

Services are not peers. The document path is a genuine pipeline and the dependencies are real:

| Layer | Services | May import |
|---|---|---|
| 3 | copilot | retrieval, and layer 1 |
| 2 | retrieval | layer 1 |
| 1 | ingestion, decision, interop, analytics | nothing |

Upward *and* sideways imports are both refused by test. Two services at the same layer importing
each other is how a stack becomes a graph, and a graph has no safe start-up order.

---

## 3. The end-to-end workflow

`ClinicalPipeline` runs a document through ten stages across four services:

```
parse → metadata → chunk        (ingestion)
  → embed → vector_store        (retrieval)
  → knowledge_graph             (knowledge_graph)
  → retrieve                    (retrieval)
  → copilot                     (copilot)
  → decision                    (decision)
  → analytics                   (analytics, optional)
```

One correlation id is threaded through every stage and appears in every log line. Each stage is
timed individually, so a slow run names the stage rather than the total. Cancellation is polled
*between* stages: a cooperative check that cannot interrupt a stage mid-write, which is the
property that makes it safe.

A stage whose service is degraded is `SKIPPED`, not failed. A failed non-optional stage stops the
run; the optional analytics stage does not.

Measured end to end on a discharge summary: **6 ms**, ten stages, five chunks, five vectors, five
graph nodes, one analytics fact.

---

## 4. The HTTP surface

The route registry declares every route with its method, path, backing service, operation,
required scope, and — for anonymous routes — the reason it is anonymous. It is validated against
the container at startup, which catches three things no per-service test can:

- **duplicate** — two services claiming one path; the second mount wins silently
- **dead** — a route naming a service that is not registered; 500 on first call
- **unreachable** — a registered, healthy service with no route

The third is not hypothetical. Phases 6 and 7 each shipped a complete API — nine FHIR operations
and eight analytics operations — that was implemented, tested, reviewed, and mounted nowhere.

**Route order is load-bearing.** `/v1/fhir/$export` and `/v1/fhir/{resource_type}` are the same
route to a router. A request for the operation reaches `search(resource_type="$export")`, which
answers a plausible 404. Nothing errors; the endpoint is gone. The registry checks that literals
precede the templates that would swallow them, and the application mounts in registry order.
That check found three shadowed FHIR operations on its first run.

Rationale: [ADR-0038](../design/adr-0038-declared-route-registry.md).

### Identity comes from the credential

The adapter builds each service's request from the authenticated `Principal` and from nothing
else. The organisation a consent decision is evaluated against is `Principal.tenant_id`; the
FHIR scopes a disclosure is checked against are derived from the principal's verified platform
scopes through an explicit table.

Both were briefly read from request headers, and both were privilege escalations: an
`x-organization-id` header is a cross-tenant read, since the organisation is what consent is
evaluated against; an `x-granted-scopes` header lets a caller send `system/*.read` and have the
consent layer honour it. Neither showed up in a test, because every test was sending the headers
it was supposed to. A test that only exercises the honest client cannot find a bug that needs a
dishonest one.

Purpose of use is never defaulted. An adapter that supplies a sensible `TREAT` has silently
disabled the consent engine ([ADR-0028](../design/adr-0028-consent-deny-by-default.md)).

---

## 5. Startup validation

Four checks, run before the first request:

| Check | Failure moved from | to |
|---|---|---|
| Configuration | first request needing the setting | process start |
| Dependency graph | a cycle discovered at runtime | process start |
| Routes | 500 on a dead route | process start |
| Wiring | first request traversing the path | process start |

The environment decides strictness. Development tolerates a default de-identification salt and
an in-memory queue, because the alternative is that nobody can run anything locally. Production
tolerates neither: the salt is fatal, because pseudonyms are `HMAC(salt, identifier)` and a salt
committed to the repository is a salt everyone knows — it makes a dataset built specifically to
be de-identifiable trivially re-identifiable by anyone holding candidate MRNs.

Validation reports **every** failure rather than stopping at the first. An operator restarting on
each successive error learns one problem per crash loop.

---

## 6. Deployment validation

`cip_gateway.deployment` reads the Dockerfile, the compose file, the nine Kubernetes manifests,
and the CI workflow, and asserts 56 properties: non-root user, pinned images, resource requests
and limits, all three probe types on the serving deployment, `runAsNonRoot`,
`allowPrivilegeEscalation: false`, a NetworkPolicy, no inline Secret values, and the CI gates.

The check that earns the module is **package drift**: the expected source-tree list is derived
from the repository rather than hardcoded, so a service added in a later phase is covered the day
it is added. It found that the image shipped six of nine packages — an image that builds, starts,
passes its health check, and cannot import a third of the platform.

Checks that need infrastructure are reported as `unverified`, never as passing. See §8.

Rationale: [ADR-0039](../design/adr-0039-configuration-is-one-contract.md).

---

## 7. Configuration as one contract

Two settings systems read `CIP_ENVIRONMENT` with disjoint vocabularies — `prod` and
`production` — while every deployment asset set the long form. Every containerised start failed
at settings load, and no test set the variable.

Secrets arrive as a mounted directory of files, which is the right shape: files rotate without a
restart, do not appear in `/proc/<pid>/environ`, are not inherited by child processes, and do not
land in a crash dump. `cip_core.secrets` maps each file to the variable the settings classes
already read, in the composition root, before any settings are constructed — so neither settings
system needs to learn about files.

The Secret manifest, the loader map, and the settings fields must agree three ways, and a test
asserts it. Before that test the manifest declared three keys that corresponded to no field.

---

## 8. What this phase did not verify

Docker, Kubernetes, PostgreSQL, MongoDB, Neo4j, and Redis were not available. Everything that can
run in-process does; everything else is named:

| Not verified | Needs |
|---|---|
| `docker build`, `compose up` | a Docker daemon |
| `kubectl apply --dry-run=server` | a cluster and its admission controllers |
| image vulnerability scan | a registry and a scanner |
| PostgreSQL row-level security | live PostgreSQL (`CIP_RUN_INTEGRATION=1`) |
| MongoDB Atlas vector search | an Atlas cluster |
| Neo4j graph traversal | live Neo4j |
| cluster-wide rate limiting | live Redis — the in-process limiter is **per replica**, so the configured 600/min per tenant is enforced independently by each pod |

The last is a real production gap, not a testing inconvenience, and startup validation warns
about it: a limit that loosens every time the deployment scales is loosest exactly when it is
most needed.

---

## 9. What this phase changed elsewhere

Phase 8 was not supposed to modify earlier phases, and it modified five things. Each was a
genuine production bug rather than a preference:

| File | Change | Why |
|---|---|---|
| `docker/Dockerfile` | copy all nine packages | the image could not import three services |
| `docker/docker-compose.yml` | pin Prometheus and Grafana, add healthchecks | unpinned observability |
| `deploy/k8s/01-configmap.yaml` | add every `cip_core` setting | the pod could not start |
| `deploy/k8s/02-secrets.yaml` | reconcile keys to real settings fields | three keys read by nothing |
| `libs/cip_core/config.py`, `libs/cip_platform/config.py` | accept both environment vocabularies | every containerised start failed |
| `cip_gateway/container.py` | `stop()` reports every service | a clean shutdown of ten printed "0 stopped" |

---

## 10. The lesson, stated plainly

Across Phases 5, 6, 7, and 8 the same pattern held: **the serious defects were found by running
the system, not by testing its parts.** Phase 6's consent bypass, Phase 7's disclosure-control
failures, and Phase 8's five deployment blockers were all invisible to suites that passed
throughout.

Phase 8 sharpens it. Every defect here lived in a *correspondence* — between an image and a
repository, between two settings vocabularies, between a mounted secret and the code that reads
it, between a declared API and a mounted one. A correspondence has no owner. It belongs to no
module, appears in no service's tests, and is nobody's job until it fails.

The response is to give each one an artefact that checks it, and to make that artefact fail at
startup rather than in production. That is what this phase is.
