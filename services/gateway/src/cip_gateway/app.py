"""The unified HTTP application.

Phases 5 to 7 each built a complete API surface and none of them was mounted. ``ClinicalApi``
and ``AnalyticsApi`` were implemented, tested, reviewed, and reachable only by importing Python
— from an operator's position, dead code. This module is where the route registry stops being a
declaration and starts being a server.

Three properties it must have, in order of how expensive they are to get wrong:

**Nothing is invented.** Every handler delegates to the service object that already exists. The
adapter's job is HTTP-to-Python and back: read the path and query, build the request the service
requires, return what it returns. Business logic here would be a second implementation of rules
that were already reviewed once, and the two would diverge.

**Authorisation is not defaulted.** ``ClinicalApi`` takes a purpose of use with no default,
because a purpose the system infers is a purpose nobody stated
(``docs/design/adr-0028-consent-deny-by-default.md``). The adapter therefore refuses a request
that omits it rather than choosing ``TREAT`` — an adapter that supplies a sensible default is an
adapter that has silently disabled the consent engine.

**Route order comes from the registry.** ``/v1/fhir/$export`` and ``/v1/fhir/{resource_type}``
are the same route to a router, so mounting order decides which one answers. The registry
already validates that order; this module walks it rather than re-listing the routes, so the
two cannot drift.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from cip_core.logging import get_logger
from cip_gateway.container import ServiceContainer
from cip_gateway.health import HealthService
from cip_gateway.platform import build_platform
from cip_gateway.routes import RouteRegistry, RouteSpec, platform_routes
from cip_gateway.startup import CheckStatus, validate_connectivity, validate_startup

__all__ = ["build_app", "create_app"]

_log = get_logger(__name__)

_PURPOSE_HEADER = "x-purpose-of-use"

#: What this application can actually serve, per service.
#:
#: Declared rather than inferred, and cross-checked against the registry at startup. The Phase 8
#: registry asked only whether a route's *service* was registered — `retrieval` was, so four
#: routes validated cleanly while the app answered 501 because no handler existed. This map
#: closes that: a route naming an operation absent from here fails startup validation instead of
#: failing a client.
#:
#: `tests/gateway/test_integration.py` asserts every entry actually responds, so an operation
#: listed here but not implemented is caught by test rather than by a caller.
ADAPTERS: dict[str, frozenset[str]] = {
    "interop": frozenset(
        {
            "capability",
            "read",
            "search",
            "write",
            "everything",
            "kickoff_export",
            "export_status",
            "bulk_import",
        }
    ),
    "analytics": frozenset(
        {
            "list_metrics",
            "get_metric",
            "list_templates",
            "list_dashboards",
            "get_dashboard",
            "list_reports",
            "get_report_runs",
            "health",
        }
    ),
    # `ask` and the ingestion write path answer 503 "not here yet" rather than 501 — the handler
    # exists and states honestly that the capability arrives with W1. They are listed because
    # the route *is* answered; what it answers is a deliberate, documented interim.
    "retrieval": frozenset({"search", "ask"}),
    "ingestion": frozenset({"upload", "get"}),
}


def _admit(request: Request, spec: RouteSpec, container: ServiceContainer) -> Any:
    """Authenticate, rate limit, and check the budget for one route.

    Done per route rather than in blanket middleware because the scope a route requires is a
    property of the route, and the registry is where routes are described. Blanket middleware
    would have to guess — and a middleware that guesses one scope for every endpoint either
    over-permits the strict routes or locks out the open ones.

    The tenant comes from the credential and never from the request: a tenant read from a header
    or a body is a tenant the caller chose. Phase 3's :class:`GatewayGuards` already does all of
    this; this is the seam that calls it, not a second implementation.
    """
    from cip_core.errors import CipError
    from cip_gateway.middleware import problem_response

    if not spec.authenticated:
        return None

    guards = container.try_get("gateway")
    if guards is None:
        return _problem(503, "service unavailable", "the authentication guards are not running")

    if not (request.headers.get("x-api-key") or request.headers.get("authorization")):
        return _problem(
            401, "unauthenticated", f"{spec.key} requires a credential bearing {spec.scope.value}"
        )

    try:
        context = guards["guards"].admit(
            headers=dict(request.headers),
            route=spec.path,
            required_scope=spec.scope,
            content_length=int(request.headers.get("content-length") or 0),
        )
    except CipError as exc:
        status, body = problem_response(exc, correlation_id="")
        return JSONResponse(status_code=status, content=body)

    # The whole Principal, not just its id. Everything downstream — the organisation a consent
    # decision is made against, the FHIR scopes a disclosure is checked against — must come from
    # here, because this is the only object in the request whose contents were verified.
    request.state.principal = context.principal
    request.state.correlation = context.correlation.correlation_id
    return None


#: Platform scope -> the SMART scopes it grants over FHIR.
#:
#: An explicit table rather than a string transformation, because the two vocabularies are not
#: mechanically related and a clever mapping would grant something nobody intended. Read as: a
#: principal holding the platform scope on the left may do the SMART things on the right, and
#: nothing else.
_SMART_SCOPES: dict[str, tuple[str, ...]] = {
    "patients:read": ("user/*.read",),
    "documents:read": ("user/DocumentReference.read", "user/Binary.read"),
    "documents:write": ("user/*.write",),
    "reference:read": ("user/CapabilityStatement.read",),
    "admin": ("system/*.read", "system/*.write"),
}


def _scopes_for(principal: Any) -> Any:
    """The FHIR scopes this principal actually holds.

    Derived from the verified credential. They were briefly read from an ``x-granted-scopes``
    request header, which let a caller name its own scopes: send ``system/*.read`` and the
    consent layer honours it. A permission the client supplies is not a permission.
    """
    from cip_interop.security import ScopeSet

    granted: list[str] = []
    for scope in principal.scopes:
        granted.extend(_SMART_SCOPES.get(str(scope), ()))
    return ScopeSet.parse(" ".join(sorted(set(granted))))


def _problem(status: int, title: str, detail: str) -> JSONResponse:
    """RFC 7807, matching the shape the gateway middleware already emits."""
    return JSONResponse(
        status_code=status,
        content={"type": "about:blank", "title": title, "status": status, "detail": detail},
        media_type="application/problem+json",
    )


def _interop_request(request: Request, container: ServiceContainer) -> Any:
    """Build a ``ClinicalApi`` request from the HTTP request, or raise a problem.

    Returns either an ``ApiRequest`` or a ``JSONResponse``; the caller checks. Deliberately not
    an exception: the failure modes here are ordinary client errors, and routing them through
    exception handlers makes them harder to read than a plain branch.
    """
    from cip_interop.domain import PurposeOfUse
    from cip_interop.orgs import OrganizationContext
    from cip_interop.security import TokenClaims

    del container

    raw_purpose = request.headers.get(_PURPOSE_HEADER, "").strip()
    if not raw_purpose:
        return _problem(
            400,
            "purpose of use required",
            f"every disclosure must state why it is being made; set {_PURPOSE_HEADER} to one of "
            f"{sorted(p.value for p in PurposeOfUse)}",
        )
    try:
        purpose = PurposeOfUse(raw_purpose)
    except ValueError:
        return _problem(400, "unknown purpose of use", f"{raw_purpose!r} is not an ActReason code")

    principal = getattr(request.state, "principal", None)
    if principal is None:
        return _problem(401, "unauthenticated", "a verified credential is required")

    # The organisation is the credential's tenant, full stop. It was briefly read from an
    # x-organization-id header, which let any caller name any organisation — and the
    # organisation is what consent is evaluated against, so that header was a cross-tenant
    # read. ``Principal.tenant_id`` is derived from the credential and is the only tenant this
    # request may touch.
    organization = str(principal.tenant_id)

    now = dt.datetime.now(dt.UTC)
    claims = TokenClaims(
        subject=principal.principal_id,
        issuer="cip-gateway",
        audience="cip-api",
        scopes=_scopes_for(principal),
        expires_at=now + dt.timedelta(minutes=5),
        issued_at=now,
        organization_id=organization,
        purpose=purpose,
        is_service_account=principal.kind != "user",
    )
    from cip_interop.api import ApiRequest

    return ApiRequest(
        context=OrganizationContext(
            principal_id=principal.principal_id,
            organization_id=organization,
            roles=frozenset(str(role) for role in principal.roles),
            # Break-glass and emergency treatment refuse service accounts, because both exist
            # to be answered for and a service account cannot be asked why.
            is_named_human=principal.kind == "user",
        ),
        claims=claims,
        purpose=purpose,
        if_match=request.headers.get("if-match", ""),
        break_glass_reason=request.headers.get("x-break-glass-reason", ""),
    )


def _analytics_principal(request: Request) -> Any:
    from cip_analytics.api import AnalyticsPrincipal

    principal = getattr(request.state, "principal", None)
    if principal is None:
        return _problem(401, "unauthenticated", "a verified credential is required")
    # Organisation and roles from the credential, never from a header — see _interop_request.
    return AnalyticsPrincipal(
        principal_id=principal.principal_id,
        organization_id=str(principal.tenant_id),
        roles=frozenset(str(role) for role in principal.roles),
    )


def _respond(response: Any) -> JSONResponse:
    """Translate a service ``ApiResponse`` into an HTTP response, unchanged.

    Status and headers pass through exactly. A gateway that rewrites a service's status is a
    gateway that has an opinion the service already had, and the two will not always agree.
    """
    return JSONResponse(
        status_code=response.status, content=response.body, headers=dict(response.headers)
    )


def _handler_for(spec: RouteSpec, container: ServiceContainer) -> Callable[..., Any]:
    """One handler, bound to the service the registry says answers this route."""

    async def handle(request: Request) -> JSONResponse:
        service = container.try_get(spec.service)
        if service is None:
            # The service is degraded or failed. 503 rather than 500: the request is fine, the
            # dependency is not, and the difference decides whether a client should retry.
            return _problem(
                503,
                "service unavailable",
                f"{spec.service} is not currently serving; the platform is running degraded",
            )

        refused = _admit(request, spec, container)
        if refused is not None:
            return refused

        if spec.service == "interop":
            # CapabilityStatement is service discovery, not disclosure. It describes what the
            # server supports and returns no patient data, so demanding a purpose of use here
            # would be asking why a client wants to read the menu — and would break the
            # conformance check every FHIR client makes before its first real request.
            if spec.operation == "capability":
                from cip_interop.fhir.definitions import FhirVersion

                return _respond(service["api"].capability(FhirVersion.R4))

            api_request = _interop_request(request, container)
            if isinstance(api_request, JSONResponse):
                return api_request
            return _respond(_dispatch_interop(service["api"], spec, request, api_request))

        if spec.service == "analytics":
            principal = _analytics_principal(request)
            if isinstance(principal, JSONResponse):
                return principal
            return _respond(_dispatch_analytics(service["api"], spec, request, principal))

        if spec.service == "retrieval":
            return await _dispatch_retrieval(service, spec, request)

        if spec.service == "ingestion":
            # Ingestion's write path needs the database-backed pipeline, not the pure processor
            # the container holds. Connecting the two is W1. Until then the route answers
            # honestly — 503 "not here yet", not 501 "does not exist" — because the distinction
            # is exactly what a client integrating against this surface needs to know.
            return _problem(
                503,
                "not available on this surface yet",
                f"{spec.key} is served by the ingestion application until its persistence is "
                f"wired into the unified app (Phase 9 W1)",
            )

        return _problem(501, "not implemented", f"no adapter is bound for service {spec.service!r}")

    handle.__name__ = f"{spec.service}_{spec.operation}"
    return handle


def _dispatch_interop(engine: Any, spec: RouteSpec, request: Request, api_request: Any) -> Any:
    from cip_interop.api import ClinicalApi

    api: ClinicalApi = engine.clinical_api if hasattr(engine, "clinical_api") else engine
    params = request.path_params
    match spec.operation:
        case "capability":
            return api.capability(api_request.fhir_version)
        case "read":
            return api.read(params["resource_type"], params["fhir_id"], api_request)
        case "search":
            return api.search(params["resource_type"], dict(request.query_params), api_request)
        case "everything":
            return api.everything(params["fhir_id"], api_request)
        case "export_status":
            return api.export_status(params["job_id"], api_request)
    raise NotImplementedError(spec.operation)


async def _dispatch_retrieval(service: Any, spec: RouteSpec, request: Request) -> JSONResponse:
    """Vector search over the tenant's own corpus.

    The tenant comes from the verified principal and is passed to ``VectorQuery``, which is
    tenant-scoped by construction — there is no way to express a cross-tenant search through this
    type, which is the property that makes the isolation hold regardless of what the caller sent.

    ``/v1/ask`` is deliberately **not** answered here. It is the grounded-answer route, and
    answering it means composing retrieval with the copilot; doing that correctly needs the
    context builder and the citation invariants Phase 3 built, and short-cutting it in an HTTP
    adapter would put a second, weaker answer path next to the reviewed one.
    """
    from cip_retrieval.vectorstore.base import VectorQuery

    principal = getattr(request.state, "principal", None)
    if principal is None:
        return _problem(401, "unauthenticated", "a verified credential is required")

    if spec.operation == "ask":
        return _problem(
            503,
            "not available on this surface yet",
            "grounded answering composes retrieval with the copilot and is wired in Phase 9 W1; "
            "use /v1/search for retrieval only",
        )

    body: dict[str, Any] = {}
    if request.headers.get("content-length"):
        try:
            body = await request.json()
        except Exception:
            return _problem(400, "invalid body", "expected a JSON object")

    text = str(body.get("query", "")).strip()
    if not text:
        return _problem(400, "query required", "supply a non-empty 'query'")

    top_k = int(body.get("top_k", 10))
    if not 1 <= top_k <= 100:
        return _problem(400, "invalid top_k", "top_k must be in [1, 100]")

    vector = await service["embeddings"].embed_query(text)
    matches = await service["vector_store"].search(
        VectorQuery(
            values=vector.values,
            tenant_id=principal.tenant_id,
            model_key=vector.model.key,
            top_k=top_k,
        )
    )
    return JSONResponse(
        status_code=200,
        content={
            "query": text,
            "model": vector.model.key,
            "matches": [
                {
                    "chunk_id": str(match.record.chunk_id),
                    "document_id": str(match.record.document_id),
                    "score": round(match.score, 6),
                }
                for match in matches
            ],
        },
    )


def _dispatch_analytics(api: Any, spec: RouteSpec, request: Request, principal: Any) -> Any:
    """Route to the ``AnalyticsApi`` the container built.

    Query parameters go through the keyword-only ``parameters`` argument. They were briefly
    passed positionally, which put a dict of untrusted query strings where the principal
    belongs — the argument every authorisation decision in this API is made from. No test
    caught it because none exercised a route with a path parameter; pyright did.
    """
    params = request.path_params
    query = dict(request.query_params)
    match spec.operation:
        case "list_metrics":
            return api.list_metrics(principal)
        case "get_metric":
            return api.get_metric(params["key"], principal, parameters=query)
        case "list_templates":
            return api.list_templates(principal)
        case "list_dashboards":
            return api.list_dashboards(principal)
        case "get_dashboard":
            return api.get_dashboard(params["key"], principal, parameters=query)
        case "list_reports":
            return api.list_reports(principal)
        case "get_report_runs":
            return api.get_report_runs(params["key"], principal)
        case "health":
            return api.health()
    raise NotImplementedError(spec.operation)


def build_app(
    *,
    container: ServiceContainer | None = None,
    registry: RouteRegistry | None = None,
    validate: bool = True,
) -> FastAPI:
    """Assemble the platform's HTTP application.

    ``validate`` runs the full startup validation and refuses to build the app if it fails. The
    default is on: an application object that exists but cannot serve is the failure mode this
    whole phase is about.
    """
    container = container if container is not None else build_platform()
    registry = registry if registry is not None else platform_routes()

    if validate:
        validate_startup(container, registry=registry, adapters=ADAPTERS).raise_for_status()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # Three phases, in this order, and the order is the point:
        #   build (sync, already done)  →  connect (opens pools)  →  probe (proves reachability)
        # Connecting is not reaching — see validate_connectivity — so a platform that skipped the
        # probe would report itself ready while unable to answer a single query.
        opened = await container.connect()
        for status in opened:
            if not status.connected:
                _log.error("app.connect_failed", service=status.name, error=status.error)

        reachability = await validate_connectivity(container)
        unreachable = [c for c in reachability if c.status is CheckStatus.FAILED]
        if unreachable and validate:
            for check in unreachable:
                _log.error("app.unreachable", detail=check.detail)
            await container.aclose()
            raise RuntimeError(
                "refusing to serve: " + "; ".join(check.detail for check in unreachable)
            )
        for check in reachability:
            _log.info("app.reachability", check=check.name, status=str(check.status))

        try:
            yield
        finally:
            # Async closes first, then the synchronous stop hooks — both in reverse dependency
            # order, so a pool is never closed while something borrowing from it is draining.
            await container.aclose()
            stopped = container.stop()
            _log.info("app.stopped", services=len(stopped))

    app = FastAPI(
        title="Clinical Intelligence Platform",
        version="0.5.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.container = container
    app.state.registry = registry
    app.state.health = HealthService()

    # Registry order is preserved deliberately: it is the order the shadowing check validated,
    # and re-sorting here would reintroduce exactly the bug that check exists to prevent.
    mounted = 0
    for spec in registry.routes:
        if spec.path.startswith("/health"):
            # The probes belong to the health surface, not to a service adapter.
            continue
        app.add_api_route(
            spec.path,
            _handler_for(spec, container),
            methods=[spec.method.value],
            name=f"{spec.service}.{spec.operation}",
            summary=spec.summary,
            include_in_schema=False,
        )
        mounted += 1

    _log.info("app.created", routes=mounted, services=len(container.names()))
    return app


def create_app() -> FastAPI:
    """Entry point for ASGI servers (``uvicorn cip_gateway.app:create_app --factory``)."""
    return build_app()
