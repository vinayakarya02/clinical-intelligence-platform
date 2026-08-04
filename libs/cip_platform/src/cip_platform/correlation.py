"""Request correlation.

One id threaded through logs, metrics, spans, events, and background jobs, so a clinician's
report of "it was slow at 14:03" resolves to a specific request rather than a time range.

Held in a :class:`~contextvars.ContextVar` rather than passed as an argument: it would
otherwise have to be threaded through every signature in the codebase, and the one place
somebody forgot would be the place the trace breaks. ContextVars are the right tool because
they are per-task in asyncio — two concurrent requests do not see each other's id, which a
module-level global would get wrong under exactly the load where correlation matters most.
"""

from __future__ import annotations

import contextvars
import re
import uuid
from dataclasses import dataclass

__all__ = [
    "CorrelationContext",
    "current_correlation",
    "new_correlation_id",
    "sanitise_correlation_id",
    "set_correlation",
]

#: Accepted inbound ids. Deliberately narrow: the id lands in logs and metric labels, so an
#: unvalidated one is a log-injection vector and an unbounded-cardinality hazard at once.
_VALID_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


@dataclass(frozen=True, slots=True)
class CorrelationContext:
    """Identifiers for the request in flight."""

    correlation_id: str
    request_id: str
    traceparent: str = ""

    def as_log_fields(self) -> dict[str, str]:
        return {
            "correlation_id": self.correlation_id,
            "request_id": self.request_id,
        }


_current: contextvars.ContextVar[CorrelationContext | None] = contextvars.ContextVar(
    "cip_correlation", default=None
)


def new_correlation_id() -> str:
    return uuid.uuid4().hex


def sanitise_correlation_id(raw: str | None) -> str:
    """Accept a caller-supplied id, or mint one.

    A client-supplied id is genuinely useful — it lets a caller correlate its own logs with
    ours — and it is also attacker-controlled input that ends up in log lines and metric
    labels. Anything not matching the narrow pattern is replaced rather than rejected: the
    request is not wrong, only its header.
    """
    if raw and _VALID_ID.match(raw):
        return raw
    return new_correlation_id()


def set_correlation(context: CorrelationContext) -> contextvars.Token[CorrelationContext | None]:
    """Bind the context for the current task. Returns a token for restoration."""
    return _current.set(context)


def current_correlation() -> CorrelationContext | None:
    return _current.get()
