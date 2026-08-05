# ADR-0037: One composition root, and only it knows every service

**Status:** Accepted (Phase 8)

## Context

By the end of Phase 7 the platform was nine services — ingestion, retrieval, knowledge graph,
copilot, decision, interop, analytics, plus configuration and audit — each with its own suite,
its own boundary tests, and its own review. Every one of them passed.

None of them had ever been started together.

Wiring nine services revealed eight construction errors in the first hour: a factory that did
not exist, a keyword argument that had been renamed, a property being called as a method, a
constructor that had grown a required argument. Each was a two-line fix. None was findable by
any per-service test, because each service was individually correct — the errors were entirely
in the assumptions one service's caller made about another's interface.

The obvious way to wire nine services is to let them import each other where needed. That gets
the system running fastest and destroys the property that made the services worth building: if
`copilot` imports `interop` directly, the two can no longer be tested, reasoned about, deployed,
or replaced separately, and no dependency-injection framework applied later brings that back.

## Decision

**One composition root.** `cip_gateway.platform` is the single module that knows the whole
service list. It and `cip_gateway.container` are the only places permitted to import more than
one service, and a test enforces it by reading the source.

**Registration is declarative.** Each service is a name, a factory, a dependency list, and a
criticality flag. The container topologically sorts them, detects cycles, and reports the cycle
path rather than recursing until the stack overflows.

**Imports live inside factories, not at module top level.** Three reasons, and the third is the
one that matters at 3am:

- a process needing only the analytics surface does not pay for the interop imports
- a broken module takes down the service that needs it, not the whole platform
- the failure surfaces as *this service failed to start*, with its name, rather than as an
  `ImportError` during module load with a traceback into somebody else's package

**Criticality is declared per service.** Retrieval and copilot are critical: a clinical platform
that cannot answer questions is down. Analytics is not: an empty warehouse is a reporting gap,
and refusing to serve clinicians over a reporting gap is the wrong trade. A failed non-critical
service degrades and the platform serves; a failed critical service aborts startup.

**Shutdown is reverse order.** Stopping a dependency before its dependents makes the dependents
fail on the way down, and the error log then describes the wrong component.

**Services form a declared layer stack, not a flat set.** Ingestion produces chunks, retrieval
embeds and searches them, the copilot answers over what retrieval returns; those dependencies
are real and directional, and pretending otherwise would mean copying types between them. So
the rule is not *no imports* but *no upward or sideways imports*, with the layers written down:
ingestion, decision, interop, analytics at the bottom, retrieval above, copilot above that.
Sideways is banned along with upward, because two services at the same layer importing each
other is how a stack becomes a graph, and a graph has no safe start-up order.

## Consequences

The container catches wiring errors at startup rather than on the first request that happens to
traverse the broken path. In a rolling deployment that difference decides whether the new pods
fail their readiness gate and the old ones keep serving, or whether traffic shifts onto a build
that cannot work.

The cost is a real one and worth naming: the composition root is a file that must change every
time a service is added, and it is the one file that cannot be reasoned about locally. That is
the trade — one module that knows everything, so that eight others can know almost nothing.

## Alternatives considered

**A dependency-injection framework.** Rejected: the container here is roughly two hundred lines
and its failure messages name the service and the cycle. A framework would add a dependency, a
decorator vocabulary, and stack traces through its own machinery, to solve a problem that is not
hard at nine services.

**Service discovery at runtime.** Rejected for the same reason startup validation exists: a
platform that discovers at first request what it could have known at start is a platform whose
failures arrive as production 500s.

**Letting services import each other.** Rejected — see Context. It is faster once and expensive
forever.
