"""HTTP middleware: request identity, structured access logging, and error translation.

Every request gets a request id — reused from an inbound ``X-Request-ID`` when a gateway
supplied one, generated otherwise — bound into the logging context and echoed back on the
response. That id is the thread tying an API call to its pipeline logs and audit entries,
which is what makes a support question ("what happened to this upload?") answerable.

Error translation is centralised here so no handler has to remember the RFC 7807 shape,
and so an unexpected exception cannot leak an internal message or stack detail to a
client. Expected errors (:class:`~cip_core.errors.CipError`) carry their own status and
problem type; everything else becomes an opaque 500 whose detail is logged, not returned.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from cip_core.errors import CipError, ProblemDetail
from cip_core.logging import bind_log_context, clear_log_context, get_logger

__all__ = [
    "PROBLEM_CONTENT_TYPE",
    "RequestContextMiddleware",
    "cip_error_handler",
    "problem_response",
    "unhandled_error_handler",
]

_log = get_logger(__name__)

PROBLEM_CONTENT_TYPE = "application/problem+json"

REQUEST_ID_HEADER = "X-Request-ID"

#: Paths excluded from access logging. Health checks are polled continuously by
#: Kubernetes and would otherwise dominate the log volume without adding information.
_QUIET_PATHS = frozenset({"/health/live", "/health/ready", "/metrics"})


def problem_response(problem: ProblemDetail, *, request_id: str | None = None) -> JSONResponse:
    """Render a problem document as an HTTP response."""
    headers = {REQUEST_ID_HEADER: request_id} if request_id else None
    return JSONResponse(
        status_code=int(problem["status"]),
        content=dict(problem),
        media_type=PROBLEM_CONTENT_TYPE,
        headers=headers,
    )


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assigns a request id, binds logging context, and emits an access log."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or str(uuid.uuid4())
        request.state.request_id = request_id
        bind_log_context(request_id=request_id)

        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            duration_ms = (time.perf_counter() - started) * 1000
            # Logged here as well as in the exception handler because middleware sits
            # outside the handler chain: an error raised by another middleware would
            # otherwise produce no access-log line at all.
            _log.exception(
                "http.request_failed",
                method=request.method,
                path=request.url.path,
                duration_ms=round(duration_ms, 2),
            )
            clear_log_context()
            raise

        duration_ms = (time.perf_counter() - started) * 1000
        response.headers[REQUEST_ID_HEADER] = request_id

        if request.url.path not in _QUIET_PATHS:
            _log.info(
                "http.request",
                method=request.method,
                path=request.url.path,
                status_code=response.status_code,
                duration_ms=round(duration_ms, 2),
            )

        clear_log_context()
        return response


async def cip_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Render a known application error as RFC 7807."""
    assert isinstance(exc, CipError)
    request_id = getattr(request.state, "request_id", None)
    problem = exc.to_problem(instance=str(request.url.path))

    log = _log.warning if exc.status < 500 else _log.error
    log(
        "http.error",
        problem_type=exc.problem_type,
        status=exc.status,
        path=request.url.path,
        detail=exc.detail,
    )
    return problem_response(problem, request_id=request_id)


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Render an unexpected exception as an opaque 500.

    The real exception is logged with a traceback; the client receives only the request
    id, which is enough to correlate with support without disclosing internals.
    """
    request_id = getattr(request.state, "request_id", None)
    _log.exception("http.unhandled_error", path=request.url.path, error_type=type(exc).__name__)
    problem = ProblemDetail(
        type_="internal-error",
        title="Internal server error",
        status=500,
        detail="An unexpected error occurred. Quote the request id when reporting this.",
        instance=str(request.url.path),
        request_id=request_id,
    )
    return problem_response(problem, request_id=request_id)
