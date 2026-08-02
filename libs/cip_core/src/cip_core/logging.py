"""Structured logging.

Every log line is structured (structlog) and carries the ambient request/tenant context
so a single request can be traced across services — the tracing requirement in
docs/architecture/02-rag-hybrid-retrieval.md §4.

The non-obvious piece is :func:`redact_sensitive`. Clinical text is PHI, and the natural
debugging instinct (``log.info("parsed", text=chunk_text)``) writes PHI into a log
aggregator that is not covered by the same access controls as the operational store.
Rather than rely on reviewer discipline, sensitive keys are redacted in the processor
chain, so the unsafe call still produces a safe log line.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import MutableMapping
from contextvars import ContextVar
from typing import Any

import structlog

from cip_core.config import LogFormat, Settings

__all__ = [
    "REDACTED",
    "bind_log_context",
    "clear_log_context",
    "configure_logging",
    "get_logger",
    "redact_sensitive",
]

REDACTED = "[redacted]"

#: Keys whose values must never reach a log sink. Matched case-insensitively against the
#: full key and against ``_``-separated parts, so ``chunk_text`` and ``text`` both match.
_SENSITIVE_KEYS: frozenset[str] = frozenset(
    {
        "text",
        "content",
        "chunk_text",
        "body",
        "raw",
        "payload",
        "mrn",
        "ssn",
        "dob",
        "birth_date",
        "patient_name",
        "password",
        "secret",
        "token",
        "authorization",
        "api_key",
        "jwt",
        "credential",
        "credentials",
    }
)

_request_id: ContextVar[str | None] = ContextVar("cip_request_id", default=None)
_tenant_id: ContextVar[str | None] = ContextVar("cip_tenant_id", default=None)
_actor_id: ContextVar[str | None] = ContextVar("cip_actor_id", default=None)


def _is_sensitive(key: str) -> bool:
    lowered = key.lower()
    if lowered in _SENSITIVE_KEYS:
        return True
    return any(part in _SENSITIVE_KEYS for part in lowered.split("_"))


def redact_sensitive(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """structlog processor that replaces sensitive values with :data:`REDACTED`.

    Redaction is shallow-recursive into dicts because event payloads are frequently
    nested (``log.info("stage", result={"text": ...})``). Lists are not walked: a list of
    PHI strings under a non-sensitive key is a code smell that should fail review rather
    than be silently laundered, and walking every list on every log call is not free.
    """
    for key, value in list(event_dict.items()):
        if _is_sensitive(key):
            event_dict[key] = REDACTED
        elif isinstance(value, dict):
            event_dict[key] = redact_sensitive(_logger, _method, dict(value))
    return event_dict


def _inject_context(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Attach ambient request/tenant/actor identifiers to every event."""
    for key, var in (
        ("request_id", _request_id),
        ("tenant_id", _tenant_id),
        ("actor_id", _actor_id),
    ):
        value = var.get()
        if value is not None and key not in event_dict:
            event_dict[key] = value
    return event_dict


def bind_log_context(
    *,
    request_id: str | None = None,
    tenant_id: str | None = None,
    actor_id: str | None = None,
) -> None:
    """Bind ambient context for the current task/request.

    Uses :mod:`contextvars`, so concurrent requests in the same event loop do not see
    each other's context.
    """
    if request_id is not None:
        _request_id.set(request_id)
    if tenant_id is not None:
        _tenant_id.set(tenant_id)
    if actor_id is not None:
        _actor_id.set(actor_id)


def clear_log_context() -> None:
    """Reset ambient logging context."""
    _request_id.set(None)
    _tenant_id.set(None)
    _actor_id.set(None)


def configure_logging(settings: Settings) -> None:
    """Configure structlog and the stdlib root logger.

    Idempotent: safe to call from both the API app factory and the CLI, which can run in
    the same process during tests.
    """
    processors: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,
        _inject_context,
        redact_sensitive,
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]

    if settings.log_format is LogFormat.JSON:
        processors.extend(
            [structlog.processors.format_exc_info, structlog.processors.JSONRenderer()]
        )
    else:
        processors.append(structlog.dev.ConsoleRenderer(colors=False))

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[settings.log_level]
        ),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )

    # Align stdlib loggers (uvicorn, sqlalchemy, pymongo) with the configured level so
    # third-party output does not bypass the level filter.
    logging.basicConfig(
        format="%(message)s", stream=sys.stderr, level=settings.log_level, force=True
    )
    for noisy in ("uvicorn.access", "sqlalchemy.engine", "pymongo", "neo4j"):
        logging.getLogger(noisy).setLevel(max(logging.INFO, logging.getLogger().level))


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a bound structlog logger."""
    return structlog.get_logger(name)  # type: ignore[no-any-return]
