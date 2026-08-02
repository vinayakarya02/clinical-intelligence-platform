"""Tamper-evident audit records.

The Phase 0 review (finding D9) rejected "append-only by GRANT" as sufficient integrity:
a compromised or careless superuser can still rewrite history, and HIPAA §164.312(b)
evidence needs to survive that. Each audit row therefore carries ``prev_hash`` and
``row_hash``, forming a per-tenant chain — editing any historical row breaks every
subsequent hash, and :func:`verify_chain` detects it.

The hash covers a *canonical* serialisation (:func:`canonical_payload`). Canonicalisation
matters more than the hash function here: if two processes serialise the same logical row
differently — different key order, different datetime precision — the chain produces
false tamper alarms, which trains operators to ignore it. Sorted keys, explicit UTC
microsecond-truncated timestamps, and a fixed separator make the encoding deterministic
across processes and Python versions.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "GENESIS_HASH",
    "AuditRecord",
    "canonical_payload",
    "compute_row_hash",
    "verify_chain",
]

#: ``prev_hash`` for the first record in a tenant's chain. A fixed non-null sentinel
#: rather than NULL, so "first row" and "prev_hash was deleted" are distinguishable.
GENESIS_HASH = "0" * 64


@dataclass(frozen=True, slots=True)
class AuditRecord:
    """An audit event prior to persistence."""

    tenant_id: uuid.UUID
    action: str
    resource_type: str
    resource_id: str | None = None
    actor_user_id: str | None = None
    actor_service: str | None = None
    phi_accessed: bool = False
    request_context: dict[str, Any] = field(default_factory=dict)
    occurred_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))


def _normalise_timestamp(value: dt.datetime) -> str:
    """Render a timestamp deterministically in UTC.

    Naive datetimes are treated as UTC rather than rejected: a caller that forgot the
    timezone should still produce a verifiable chain, and rejecting would turn a logging
    mistake into a request failure.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC).isoformat(timespec="microseconds")


def canonical_payload(record: AuditRecord, prev_hash: str) -> str:
    """Return the canonical string whose digest becomes ``row_hash``."""
    payload = {
        "action": record.action,
        "actor_service": record.actor_service,
        "actor_user_id": record.actor_user_id,
        "occurred_at": _normalise_timestamp(record.occurred_at),
        "phi_accessed": record.phi_accessed,
        "prev_hash": prev_hash,
        "request_context": record.request_context,
        "resource_id": record.resource_id,
        "resource_type": record.resource_type,
        "tenant_id": str(record.tenant_id),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def compute_row_hash(record: AuditRecord, prev_hash: str) -> str:
    """Compute the SHA-256 chain hash for ``record`` following ``prev_hash``."""
    return hashlib.sha256(canonical_payload(record, prev_hash).encode("utf-8")).hexdigest()


def verify_chain(entries: list[tuple[AuditRecord, str, str]]) -> list[int]:
    """Verify a hash chain and return the indices of entries that fail.

    ``entries`` is an ordered list of ``(record, prev_hash, row_hash)`` as stored. Two
    distinct failures are detected: a recomputed hash that disagrees with the stored one
    (the row was altered), and a ``prev_hash`` that does not match the preceding
    ``row_hash`` (a row was inserted, removed, or reordered).

    Returns indices rather than raising so a verification job can report the full extent
    of the damage in one pass instead of stopping at the first bad row.
    """
    failures: list[int] = []
    expected_prev = GENESIS_HASH
    for index, (record, prev_hash, row_hash) in enumerate(entries):
        if prev_hash != expected_prev or compute_row_hash(record, prev_hash) != row_hash:
            failures.append(index)
        expected_prev = row_hash
    return failures
