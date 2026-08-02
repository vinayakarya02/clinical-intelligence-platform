"""Application errors as RFC 7807 Problem Details.

The API contract in docs/api/openapi.yaml specifies RFC 7807 for every error response.
Defining the taxonomy here — rather than raising ``HTTPException`` with ad hoc payloads
at each call site — keeps the wire format consistent and lets non-API callers (the CLI,
the ETL pipeline) raise the same exceptions without importing FastAPI.

``detail`` is operator-facing and must never contain PHI or clinical text: these strings
reach clients and logs. Identifiers (document ids, hashes) are fine; extracted content is
not.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "AuthenticationError",
    "AuthorizationError",
    "CipError",
    "ConfigurationError",
    "ConflictError",
    "DependencyUnavailableError",
    "DuplicateDocumentError",
    "NotFoundError",
    "PayloadTooLargeError",
    "PipelineError",
    "ProblemDetail",
    "UnsupportedMediaTypeError",
    "ValidationFailedError",
]

_PROBLEM_BASE_URI = "https://docs.cip.example.com/problems"


class ProblemDetail(dict[str, Any]):
    """RFC 7807 problem document.

    A ``dict`` subclass so it serialises directly without an extra model layer.
    """

    def __init__(
        self,
        *,
        type_: str,
        title: str,
        status: int,
        detail: str,
        instance: str | None = None,
        **extensions: Any,
    ) -> None:
        payload: dict[str, Any] = {
            "type": f"{_PROBLEM_BASE_URI}/{type_}",
            "title": title,
            "status": status,
            "detail": detail,
        }
        if instance is not None:
            payload["instance"] = instance
        payload.update(extensions)
        super().__init__(payload)


class CipError(Exception):
    """Base class for all application errors.

    Subclasses set ``status`` and ``problem_type``; the message becomes ``detail``.
    """

    status: int = 500
    problem_type: str = "internal-error"
    title: str = "Internal server error"

    def __init__(self, detail: str, **extensions: Any) -> None:
        super().__init__(detail)
        self.detail = detail
        self.extensions = extensions

    def to_problem(self, *, instance: str | None = None) -> ProblemDetail:
        return ProblemDetail(
            type_=self.problem_type,
            title=self.title,
            status=self.status,
            detail=self.detail,
            instance=instance,
            **self.extensions,
        )


class ConfigurationError(CipError):
    """Invalid or unsafe configuration. Raised at startup, not per request."""

    status = 500
    problem_type = "configuration-error"
    title = "Service misconfigured"


class AuthenticationError(CipError):
    status = 401
    problem_type = "authentication-failed"
    title = "Authentication failed"


class AuthorizationError(CipError):
    """Caller is authenticated but out of scope for the resource.

    Note the deliberate asymmetry with :class:`NotFoundError`: resource-existence
    questions are answered with 404 when the caller's tenant does not match, so the API
    cannot be used to probe which document ids exist in another tenant.
    """

    status = 403
    problem_type = "authorization-failed"
    title = "Not permitted"


class NotFoundError(CipError):
    status = 404
    problem_type = "not-found"
    title = "Resource not found"


class ConflictError(CipError):
    status = 409
    problem_type = "conflict"
    title = "Conflicting request"


class DuplicateDocumentError(ConflictError):
    """Content-identical document already ingested for this tenant.

    Carries the existing document id so a client can adopt it instead of retrying.
    """

    problem_type = "duplicate-document"
    title = "Document already ingested"

    def __init__(self, detail: str, *, existing_document_id: str, content_hash: str) -> None:
        super().__init__(
            detail, existing_document_id=existing_document_id, content_hash=content_hash
        )
        self.existing_document_id = existing_document_id
        self.content_hash = content_hash


class ValidationFailedError(CipError):
    status = 422
    problem_type = "validation-failed"
    title = "Request failed validation"

    def __init__(self, detail: str, *, violations: list[str] | None = None) -> None:
        super().__init__(detail, violations=violations or [])
        self.violations = violations or []


class UnsupportedMediaTypeError(CipError):
    status = 415
    problem_type = "unsupported-media-type"
    title = "Unsupported media type"


class PayloadTooLargeError(CipError):
    status = 413
    problem_type = "payload-too-large"
    title = "Payload too large"


class PipelineError(CipError):
    """A pipeline stage failed. ``stage`` identifies where, for triage without log diving."""

    status = 500
    problem_type = "pipeline-error"
    title = "Document processing failed"

    def __init__(self, detail: str, *, stage: str) -> None:
        super().__init__(detail, stage=stage)
        self.stage = stage


class DependencyUnavailableError(CipError):
    """A backing service (Postgres/Mongo/Neo4j/storage) is unreachable."""

    status = 503
    problem_type = "dependency-unavailable"
    title = "Dependency unavailable"

    def __init__(self, detail: str, *, dependency: str) -> None:
        super().__init__(detail, dependency=dependency)
        self.dependency = dependency
