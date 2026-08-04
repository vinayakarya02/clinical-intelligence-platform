"""Gateway middleware: the ordered chain every request passes through.

Order is the design. Each layer assumes the one outside it may have failed, and each is placed
where it costs the least while still being effective:

1. **correlation** — first, so every later log line and metric carries the id, including the
   ones emitted while rejecting the request.
2. **body limit** — before authentication, because rejecting an oversized body should not
   require first doing cryptography on it.
3. **authentication** — establishes the principal and therefore the tenant.
4. **rate limit** — after authentication, because the limit is per tenant and per principal,
   and neither is known before.
5. **budget** — after the rate limit, because it is the more expensive check and a
   rate-limited request should never reach it.
6. **metrics** — outermost in effect: it wraps everything so a rejection is measured too.

The one ordering that is *not* obvious: metrics is installed first (so it wraps everything)
but conceptually last. A metrics layer inside the rate limiter would not observe rejections,
and "how often are we rejecting" is the question a rate limit exists to answer.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from cip_core.errors import CipError
from cip_core.logging import get_logger
from cip_platform.correlation import (
    CorrelationContext,
    new_correlation_id,
    sanitise_correlation_id,
    set_correlation,
)
from cip_platform.observability.ai_metrics import AIMetrics
from cip_platform.security.identity import (
    ApiKeyStore,
    AuthenticationError,
    Principal,
    Scope,
)
from cip_platform.security.limits import (
    BudgetDecision,
    RateLimitError,
    SpendBudget,
    TokenBucketLimiter,
)

__all__ = ["GatewayContext", "GatewayGuards", "PayloadTooLargeError", "problem_response"]

_log = get_logger(__name__)

#: Header names. Correlation is accepted from the caller so a client can tie its own logs to
#: ours; it is sanitised before use because it lands in log lines and metric labels.
HEADER_CORRELATION = "x-correlation-id"
HEADER_REQUEST_ID = "x-request-id"
HEADER_API_KEY = "x-api-key"
HEADER_AUTHORIZATION = "authorization"


@dataclass(frozen=True, slots=True)
class GatewayContext:
    """Everything established about a request before the handler runs."""

    principal: Principal
    correlation: CorrelationContext
    route: str

    @property
    def tenant_id(self) -> uuid.UUID:
        """The tenant, from the credential.

        Never from the request body: a body that can name its own tenant is a body that can
        name someone else's.
        """
        return self.principal.tenant_id


def problem_response(error: Exception, *, correlation_id: str) -> tuple[int, dict[str, Any]]:
    """Render an exception as an RFC 7807 problem document.

    The correlation id is in the body, not only in a header. A clinician reporting a failure
    reads what is on screen, and an id they can quote turns "it broke" into a single log query.

    An unrecognised exception becomes a generic 500 whose detail is a type name. Internal
    messages routinely contain identifiers, table names, and query fragments, and a stack
    trace in an HTTP body is reconnaissance.
    """
    if isinstance(error, CipError):
        status = getattr(error, "status", 500)
        body = {
            "type": f"https://cip.example.com/problems/{getattr(error, 'problem_type', 'error')}",
            "title": getattr(error, "title", "Request failed"),
            "status": status,
            "detail": str(error),
            "correlation_id": correlation_id,
        }
        if isinstance(error, RateLimitError):
            body["retry_after_seconds"] = round(error.retry_after_seconds)
        return status, body

    _log.error(
        "gateway.unhandled_error",
        error=type(error).__name__,
        correlation_id=correlation_id,
    )
    return 500, {
        "type": "https://cip.example.com/problems/internal-error",
        "title": "Internal error",
        "status": 500,
        "detail": type(error).__name__,
        "correlation_id": correlation_id,
    }


class GatewayGuards:
    """The authentication, authorisation, limit, and budget checks.

    Framework-agnostic on purpose: it takes a header mapping and returns a context, so it is
    exercised in tests without an HTTP client and could sit behind any web framework. Binding
    this logic to one framework's request object is how it becomes untestable.
    """

    def __init__(
        self,
        *,
        api_keys: ApiKeyStore,
        tenant_limiter: TokenBucketLimiter,
        principal_limiter: TokenBucketLimiter,
        budget: SpendBudget,
        metrics: AIMetrics | None = None,
        max_request_bytes: int = 8 * 1024 * 1024,
    ) -> None:
        self._api_keys = api_keys
        self._tenant_limiter = tenant_limiter
        self._principal_limiter = principal_limiter
        self._budget = budget
        self._metrics = metrics
        self._max_request_bytes = max_request_bytes

    def admit(
        self,
        *,
        headers: dict[str, str],
        route: str,
        required_scope: Scope,
        content_length: int = 0,
    ) -> GatewayContext:
        """Run the full chain, returning the request context or raising."""
        lowered = {k.lower(): v for k, v in headers.items()}

        correlation = CorrelationContext(
            correlation_id=sanitise_correlation_id(lowered.get(HEADER_CORRELATION)),
            request_id=new_correlation_id(),
        )
        set_correlation(correlation)

        # Before authentication: rejecting an oversized body should not require first doing
        # cryptography on the request that carries it.
        if content_length > self._max_request_bytes:
            raise PayloadTooLargeError(
                f"Request body of {content_length} bytes exceeds the "
                f"{self._max_request_bytes}-byte limit"
            )

        principal = self._authenticate(lowered)
        principal.require(required_scope)

        # Per-principal first: it is the tighter limit, so a leaked key is stopped before it
        # can consume any of its tenant's allowance.
        try:
            self._principal_limiter.check(f"principal:{principal.principal_id}", scope="principal")
            self._tenant_limiter.check(f"tenant:{principal.tenant_id}", scope="tenant")
        except RateLimitError as exc:
            if self._metrics is not None:
                self._metrics.record_rate_limit(scope=exc.scope)
            raise

        if self._budget.check(principal.tenant_id) is BudgetDecision.REJECT:
            if self._metrics is not None:
                self._metrics.record_budget_rejection(tenant=str(principal.tenant_id))
            raise RateLimitError(
                "Daily spend budget exhausted for this tenant",
                # Points at the actual window boundary, so a client backs off to a time when
                # budget will exist rather than retrying into a wall.
                retry_after_seconds=self._budget.seconds_until_reset(),
                scope="budget",
            )

        return GatewayContext(principal=principal, correlation=correlation, route=route)

    def _authenticate(self, headers: dict[str, str]) -> Principal:
        """Identify the caller from an API key or a bearer token."""
        api_key = headers.get(HEADER_API_KEY)
        if api_key:
            return self._api_keys.authenticate(api_key)

        authorization = headers.get(HEADER_AUTHORIZATION, "")
        if authorization.lower().startswith("bearer "):
            # A JWT path exists in the platform library but needs a verifier this gateway is
            # not configured with. Refusing is correct: accepting an unverified bearer token
            # would be worse than not supporting one.
            raise AuthenticationError("Bearer tokens require a configured token verifier")

        raise AuthenticationError("No credential was presented")

    def charge(self, context: GatewayContext, cost_usd: float) -> None:
        """Record spend after a request completes."""
        if cost_usd:
            self._budget.charge(context.tenant_id, cost_usd)


class PayloadTooLargeError(CipError):
    status = 413
    problem_type = "payload-too-large"
    title = "Request body is too large"


async def timed(
    handler: Callable[[], Awaitable[Any]],
    *,
    metrics: AIMetrics | None,
    route: str,
) -> tuple[Any, str]:
    """Run a handler, recording duration and outcome.

    Records on the failure path too. A latency histogram that only sees successes hides the
    case where a dependency times out — the requests that matter most are exactly the ones a
    success-only metric drops.
    """
    started = time.perf_counter()
    status = "ok"
    try:
        result = await handler()
    except Exception as exc:
        status = getattr(exc, "status", 500)
        status = str(status)
        raise
    finally:
        if metrics is not None:
            metrics.record_request(
                route=route, status=status, duration_seconds=time.perf_counter() - started
            )
    return result, status
