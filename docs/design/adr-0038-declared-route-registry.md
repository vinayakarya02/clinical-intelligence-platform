# ADR-0038: The HTTP surface is declared, not discovered

**Status:** Accepted (Phase 8)

## Context

Six phases each grew an API. Phase 1 mounted document routes on FastAPI. Phase 6 built
`ClinicalApi` — nine FHIR operations with consent evaluation, bulk export, and an EMPI-backed
person resolver. Phase 7 built `AnalyticsApi` — eight read operations over the semantic layer
with disclosure control.

Phases 6 and 7 were never mounted. Both classes were implemented, unit-tested, adversarially
reviewed, and reachable only by importing Python. From an operator's position — someone holding
a credential and a base URL — two phases of work did not exist.

Nothing detected this because nothing was looking at the union. Each service's tests called its
API object directly and passed. The application object listed the routers it knew about and
started cleanly. There was no artefact anywhere that said what the platform's HTTP surface was
supposed to be, so there was nothing for reality to disagree with.

Three failure modes live in that gap:

**Duplicate** — two services claim the same method and path. Whichever mounts second wins,
silently, and the loser's tests still pass because they never go through HTTP.

**Dead** — a route names a service the container does not register. It returns 500 on first
call, in production.

**Unreachable** — a service is registered, started, healthy, and has no route at all.

## Decision

A **declared registry**: `cip_gateway.routes.platform_routes()` lists every route the platform
intends to serve, each with its method, path, backing service, operation, required scope, and —
for anonymous routes — the reason it is anonymous.

The registry is validated **against the container**, not derived from the running application. A
registry derived from the app can only report what is there; it cannot report what should have
been, and "should have been" is the entire class of bug this exists to catch.

Validation reports duplicates, dead routes, unreachable services, and unauthenticated routes
that state no reason. A service with no route must be listed in `INTERNAL_SERVICES` with an
explanation — settings, audit, the knowledge graph, the copilot, and the decision engine are all
legitimately internal, and saying so is cheap. A new service that is neither routed nor declared
internal fails startup.

**Registration order is part of the contract.** `/v1/fhir/$export` and `/v1/fhir/{resource_type}`
normalise to different strings, so a naive comparison sees no conflict — but a router matches in
order, and the template matches `$export` perfectly. The request reaches
`search(resource_type="$export")`, which answers a plausible 404 for an unknown resource type.
Nothing errors. The endpoint is simply gone. The registry therefore checks that every literal
path is registered before any template that would swallow it, and the application mounts routes
in registry order rather than re-listing them.

That check found three shadowed FHIR operations in this repository on its first run.

**Scope is declared on the route.** A scope that lives only inside a handler is a scope nobody
audits, and blanket middleware that applies one scope to every endpoint either over-permits the
strict routes or locks out the open ones.

## Consequences

Adding a service now requires a decision — route it, or say why not — at the moment the service
is added, rather than discovering the omission two phases later.

The registry is a second place to edit when adding a route, and it can be wrong in the direction
of declaring something that is not mounted. That is the acceptable direction: the validation runs
at startup, so a declared-but-missing route fails the readiness gate rather than a request.

## Alternatives considered

**Derive the surface from the mounted app.** Rejected: it cannot detect the unreachable case,
which is the one that actually happened, twice.

**An OpenAPI document as the source of truth.** Rejected for now: it describes shapes well and
service ownership poorly, and the ownership relation is what the dead-route and unreachable
checks are built on. The registry serialises to JSON and can generate an OpenAPI document later.
