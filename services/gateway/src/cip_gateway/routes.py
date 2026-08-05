"""The route registry: one declared surface for the whole platform.

Six services grew their own API surfaces over six phases. Each was correct in isolation, and
nothing checked the union. Three failure modes live in that gap, and all three are invisible to
per-service tests because per-service tests only ever see one service:

**Duplicate** — two services claim the same method and path. Whichever is mounted second wins,
silently, and the loser's tests still pass because they call it directly.

**Dead** — a route is declared for a service the container does not register. It returns 500 on
first call, in production, because nothing ever asked whether the two lists agreed.

**Unreachable** — a service is registered, started, healthy, and has no route. Phase 6 and
Phase 7 each shipped a complete API this way: implemented, unit-tested, and reachable only from
Python. From an operator's position it was dead code.

The registry is therefore *declarative and validated against the container* rather than derived
from whatever happens to be mounted. A registry derived from the app can only tell you what is
there; it cannot tell you what should have been.

Path shadowing is checked too. ``/v1/x/{id}`` and ``/v1/x/{key}`` are the same route to a
router, however different they look, so both are normalised before comparison.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from cip_core.logging import get_logger
from cip_gateway.container import ServiceContainer
from cip_platform.security.identity import Scope

__all__ = [
    "INTERNAL_SERVICES",
    "HttpMethod",
    "RouteIssue",
    "RouteRegistry",
    "RouteSpec",
    "platform_routes",
]

_log = get_logger(__name__)

_PARAM = re.compile(r"\{[^}]+\}")


class HttpMethod(StrEnum):
    GET = "GET"
    POST = "POST"
    PUT = "PUT"
    PATCH = "PATCH"
    DELETE = "DELETE"


#: Services that legitimately have no HTTP surface, with the reason. Declared rather than
#: inferred: a service with no routes is either an internal collaborator or an accident, and
#: only the author knows which. A new service that is neither routed nor listed here is
#: reported — which is how two phases of unreachable API were found.
INTERNAL_SERVICES: dict[str, str] = {
    "settings": "configuration, consumed by every other service",
    # Backing stores. Infrastructure the services stand on, never a surface a client addresses:
    # a route that let a caller reach the database directly would bypass every consent, tenant,
    # and disclosure control the services above it exist to enforce. Their liveness is reported
    # through the health probes, which is the right place for it.
    "postgres": "operational store; reached through repositories, never directly",
    "mongo": "artifact store; reached through repositories, never directly",
    "neo4j": "graph store; reached through the knowledge-graph service",
    "cache": "read-through cache applied inside services, not addressable",
    "queue": "background work is enqueued by services; there is no client-facing queue API",
    "events": "the event backbone is published to, not requested",
    "gateway": (
        "authentication, rate limiting, and budget — applied to every route rather than "
        "exposed as one"
    ),
    "audit": "audit sink, written through the services that disclose data",
    "knowledge_graph": "reached through retrieval and the copilot, never directly",
    "copilot": "reached through the pipeline; a direct surface would bypass retrieval",
    "decision": "reached through the pipeline, which supplies the clinical context",
}


@dataclass(frozen=True, slots=True)
class RouteSpec:
    """One HTTP operation and the service that answers it."""

    method: HttpMethod
    path: str
    service: str
    operation: str
    summary: str = ""
    authenticated: bool = True
    public_reason: str = ""
    """Why this route is anonymous, required whenever ``authenticated`` is False.

    A route without a credential check is the kind of thing that gets added for a good reason
    and then outlives it. Forcing the reason into the declaration means the next reader can
    judge whether it still holds, and the validator refuses an unauthenticated route that does
    not state one."""
    scope: Scope = Scope.PATIENTS_READ
    """The permission this route requires.

    Declared here rather than checked inside the handler so the requirement is visible in the
    registry, testable without an HTTP client, and impossible to forget when a route is added —
    a scope that lives only in a handler is a scope nobody audits."""

    @property
    def key(self) -> str:
        return f"{self.method.value} {self.path}"

    @property
    def shape(self) -> str:
        """The path as a router sees it — parameter names erased."""
        return f"{self.method.value} {_PARAM.sub('{}', self.path)}"

    def to_json(self) -> dict[str, Any]:
        return {
            "method": str(self.method),
            "path": self.path,
            "service": self.service,
            "operation": self.operation,
            "summary": self.summary,
            "authenticated": self.authenticated,
            "publicReason": self.public_reason,
            "scope": str(self.scope),
        }


class IssueKind(StrEnum):
    DUPLICATE = "duplicate"
    UNIMPLEMENTED = "unimplemented"
    SHADOWED = "shadowed"
    DEAD = "dead"
    UNREACHABLE = "unreachable"
    UNAUTHENTICATED = "unauthenticated"


@dataclass(frozen=True, slots=True)
class RouteIssue:
    kind: IssueKind
    detail: str

    def render(self) -> str:
        return f"[{self.kind.value:<15}] {self.detail}"

    def to_json(self) -> dict[str, str]:
        return {"kind": str(self.kind), "detail": self.detail}


class RouteRegistry:
    """Every route the platform intends to serve."""

    def __init__(self) -> None:
        self._routes: list[RouteSpec] = []

    def add(self, spec: RouteSpec) -> RouteRegistry:
        self._routes.append(spec)
        return self

    def extend(self, specs: list[RouteSpec]) -> RouteRegistry:
        self._routes.extend(specs)
        return self

    @property
    def routes(self) -> tuple[RouteSpec, ...]:
        return tuple(self._routes)

    def for_service(self, service: str) -> tuple[RouteSpec, ...]:
        return tuple(route for route in self._routes if route.service == service)

    def services(self) -> frozenset[str]:
        return frozenset(route.service for route in self._routes)

    def validate(
        self,
        container: ServiceContainer | None = None,
        adapters: dict[str, frozenset[str]] | None = None,
    ) -> tuple[RouteIssue, ...]:
        """Every disagreement between the routes, the services, and the handlers.

        ``adapters`` maps a service name to the operations it can actually serve over HTTP.
        Without it, this check asks only whether the backing *service is registered* — and that
        was a real hole: ``retrieval`` was registered, so four routes declaring it passed
        validation while the application answered 501, because no handler existed. A service
        being alive is not the same claim as a route being answerable.
        """
        issues: list[RouteIssue] = []

        by_shape: dict[str, list[RouteSpec]] = defaultdict(list)
        for route in self._routes:
            by_shape[route.shape].append(route)

        for shape, group in sorted(by_shape.items()):
            if len(group) < 2:
                continue
            exact = len({route.path for route in group}) == 1
            kind = IssueKind.DUPLICATE if exact else IssueKind.SHADOWED
            owners = ", ".join(f"{r.service}.{r.operation}" for r in group)
            detail = (
                f"{shape} claimed by {len(group)} routes ({owners})"
                if exact
                else f"{shape} — paths {sorted({r.path for r in group})} differ only in "
                f"parameter names and resolve identically ({owners})"
            )
            issues.append(RouteIssue(kind, detail))

        issues.extend(self._shadowing_issues())

        for route in self._routes:
            if route.authenticated or route.public_reason:
                continue
            issues.append(
                RouteIssue(
                    IssueKind.UNAUTHENTICATED,
                    f"{route.key} is unauthenticated and gives no reason; set public_reason if "
                    f"anonymous access is intended",
                )
            )

        if container is None:
            return tuple(issues)

        registered = frozenset(container.names())
        for service in sorted(self.services() - registered):
            routes = ", ".join(r.key for r in self.for_service(service))
            issues.append(
                RouteIssue(
                    IssueKind.DEAD,
                    f"service {service!r} is routed but not registered — {routes} would 500",
                )
            )

        if adapters is not None:
            for route in self._routes:
                if route.path.startswith("/health"):
                    continue  # served by the health surface, not by a service adapter
                implemented = adapters.get(route.service, frozenset())
                if route.operation not in implemented:
                    issues.append(
                        RouteIssue(
                            IssueKind.UNIMPLEMENTED,
                            f"{route.key} declares {route.service}.{route.operation} and no "
                            f"handler implements it; the route would answer 501",
                        )
                    )

        routed = self.services()
        for service in sorted(registered - routed - frozenset(INTERNAL_SERVICES)):
            issues.append(
                RouteIssue(
                    IssueKind.UNREACHABLE,
                    f"service {service!r} starts and is healthy but has no route; add one or "
                    f"declare it in INTERNAL_SERVICES with a reason",
                )
            )
        return tuple(issues)

    def _shadowing_issues(self) -> list[RouteIssue]:
        """Literal paths swallowed by an earlier template.

        The dangerous asymmetry: ``/v1/fhir/$export`` and ``/v1/fhir/{resource_type}`` normalise
        to different strings, so the shape comparison above sees no conflict — but a router
        matches in registration order, and the template matches ``$export`` perfectly. The
        request reaches ``search(resource_type="$export")``, which answers a plausible 404 for
        an unknown resource type. Nothing errors. The endpoint is simply gone.

        Registration order is therefore part of the contract, and this is where it is checked.
        """
        issues: list[RouteIssue] = []
        for specific_index, specific in enumerate(self._routes):
            for general_index, general in enumerate(self._routes):
                if general_index >= specific_index or general.method is not specific.method:
                    continue
                if _shadows(general.path, specific.path):
                    issues.append(
                        RouteIssue(
                            IssueKind.SHADOWED,
                            f"{specific.key} ({specific.service}.{specific.operation}) is "
                            f"unreachable: {general.key} ({general.service}.{general.operation}) "
                            f"is registered earlier and matches it — register the literal path "
                            f"before the templated one",
                        )
                    )
        return issues

    def to_json(self) -> list[dict[str, Any]]:
        return [route.to_json() for route in sorted(self._routes, key=lambda r: (r.path, r.method))]

    def render(self) -> str:
        lines = [f"{len(self._routes)} route(s)"]
        for route in sorted(self._routes, key=lambda r: (r.service, r.path, r.method)):
            lines.append(f"  {route.method.value:<6} {route.path:<46} {route.service}")
        return "\n".join(lines)


def _shadows(general: str, specific: str) -> bool:
    """Does ``general`` match every request ``specific`` was written for?

    True when the paths have the same segment count and each segment of ``general`` is either
    identical to the corresponding one in ``specific`` or a parameter — and at least one is a
    parameter standing over a literal. Equal paths are handled by the duplicate check, not here.
    """
    left, right = general.strip("/").split("/"), specific.strip("/").split("/")
    if len(left) != len(right):
        return False
    generalised = False
    for general_segment, specific_segment in zip(left, right, strict=True):
        general_param = general_segment.startswith("{")
        specific_param = specific_segment.startswith("{")
        if general_param and not specific_param:
            generalised = True
        elif general_param and specific_param:
            continue
        elif general_segment != specific_segment:
            return False
    return generalised


def _ingestion_routes() -> list[RouteSpec]:
    return [
        RouteSpec(
            HttpMethod.POST,
            "/v1/documents",
            "ingestion",
            "upload",
            "Upload a document",
            scope=Scope.DOCUMENTS_WRITE,
        ),
        RouteSpec(
            HttpMethod.GET,
            "/v1/documents/{document_id}",
            "ingestion",
            "get",
            "Fetch one",
            scope=Scope.DOCUMENTS_READ,
        ),
        RouteSpec(
            HttpMethod.GET,
            "/health/live",
            "ingestion",
            "live",
            "Liveness",
            authenticated=False,
            public_reason="kubelet probe; no credential exists at probe time",
        ),
        RouteSpec(
            HttpMethod.GET,
            "/health/ready",
            "ingestion",
            "ready",
            "Readiness",
            authenticated=False,
            public_reason="kubelet probe; no credential exists at probe time",
        ),
    ]


def _interop_routes() -> list[RouteSpec]:
    """Phase 6's ClinicalApi, which had no HTTP surface until this phase.

    **Order is load-bearing.** FHIR puts system-level operations (``$export``, ``$import``) and
    ``metadata`` in the same position as a resource type, so ``/v1/fhir/{resource_type}`` matches
    all of them. Every literal path is therefore declared before the templated ones, and the
    registry's shadowing check fails the build if that is ever reversed — which it caught here on
    its first run.
    """
    return [
        RouteSpec(
            HttpMethod.GET,
            "/v1/fhir/metadata",
            "interop",
            "capability",
            "Capability",
            authenticated=False,
            public_reason="FHIR conformance discovery; describes the server, discloses no "
            "patient data, and every client reads it before it can authenticate meaningfully",
            scope=Scope.REFERENCE_READ,
        ),
        RouteSpec(
            HttpMethod.POST,
            "/v1/fhir/$export",
            "interop",
            "kickoff_export",
            "Bulk export",
            scope=Scope.DOCUMENTS_WRITE,
        ),
        RouteSpec(
            HttpMethod.GET, "/v1/fhir/$export/{job_id}", "interop", "export_status", "Export status"
        ),
        RouteSpec(
            HttpMethod.POST,
            "/v1/fhir/$import",
            "interop",
            "bulk_import",
            "Bulk import",
            scope=Scope.DOCUMENTS_WRITE,
        ),
        RouteSpec(
            HttpMethod.GET,
            "/v1/fhir/Patient/{fhir_id}/$everything",
            "interop",
            "everything",
            "Patient compartment",
        ),
        RouteSpec(HttpMethod.GET, "/v1/fhir/{resource_type}/{fhir_id}", "interop", "read", "Read"),
        RouteSpec(HttpMethod.GET, "/v1/fhir/{resource_type}", "interop", "search", "Search"),
        RouteSpec(
            HttpMethod.POST,
            "/v1/fhir/{resource_type}",
            "interop",
            "write",
            "Create",
            scope=Scope.DOCUMENTS_WRITE,
        ),
    ]


def _analytics_routes() -> list[RouteSpec]:
    """Phase 7's AnalyticsApi, likewise."""
    return [
        RouteSpec(
            HttpMethod.GET,
            "/v1/analytics/metrics",
            "analytics",
            "list_metrics",
            "Metrics",
            scope=Scope.ANALYTICS_READ,
        ),
        RouteSpec(
            HttpMethod.GET,
            "/v1/analytics/metrics/{key}",
            "analytics",
            "get_metric",
            "One metric",
            scope=Scope.ANALYTICS_READ,
        ),
        RouteSpec(
            HttpMethod.GET,
            "/v1/analytics/templates",
            "analytics",
            "list_templates",
            "Templates",
            scope=Scope.ANALYTICS_READ,
        ),
        RouteSpec(
            HttpMethod.GET,
            "/v1/analytics/dashboards",
            "analytics",
            "list_dashboards",
            "Boards",
            scope=Scope.ANALYTICS_READ,
        ),
        RouteSpec(
            HttpMethod.GET,
            "/v1/analytics/dashboards/{key}",
            "analytics",
            "get_dashboard",
            "One board",
            scope=Scope.ANALYTICS_READ,
        ),
        RouteSpec(
            HttpMethod.GET,
            "/v1/analytics/reports",
            "analytics",
            "list_reports",
            "Reports",
            scope=Scope.ANALYTICS_READ,
        ),
        RouteSpec(
            HttpMethod.GET,
            "/v1/analytics/reports/{key}/runs",
            "analytics",
            "get_report_runs",
            "Report runs",
            scope=Scope.ANALYTICS_READ,
        ),
        RouteSpec(
            HttpMethod.GET,
            "/v1/analytics/health",
            "analytics",
            "health",
            "Warehouse freshness",
            scope=Scope.ANALYTICS_READ,
        ),
    ]


def _retrieval_routes() -> list[RouteSpec]:
    return [
        RouteSpec(
            HttpMethod.POST,
            "/v1/search",
            "retrieval",
            "search",
            "Hybrid retrieval",
            scope=Scope.DOCUMENTS_READ,
        ),
        RouteSpec(
            HttpMethod.POST,
            "/v1/ask",
            "retrieval",
            "ask",
            "Grounded answer",
            scope=Scope.COPILOT_ASK,
        ),
    ]


def platform_routes() -> RouteRegistry:
    """The platform's whole HTTP surface, in one list."""
    return (
        RouteRegistry()
        .extend(_ingestion_routes())
        .extend(_retrieval_routes())
        .extend(_interop_routes())
        .extend(_analytics_routes())
    )
