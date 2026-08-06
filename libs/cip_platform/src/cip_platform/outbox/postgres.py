"""The outbox in PostgreSQL.

Two operations, and they belong to different owners:

**Append** takes the *caller's* session and does not commit. That is the entire pattern. An
append that opened its own transaction would commit the event separately from the business data
it describes, which is the dual-write it exists to eliminate — with an extra table to maintain
and nothing gained. The signature makes the requirement structural: there is no way to append
without already being inside somebody's transaction.

**Claim** runs in the relay's own transaction and is where concurrency is decided.

```sql
WITH heads AS (
    SELECT DISTINCT ON (partition_key) event_id, next_attempt_at
    FROM outbox_events
    WHERE status = 'pending'
    ORDER BY partition_key, sequence_id      -- the head of each partition
)
SELECT * FROM outbox_events
WHERE event_id IN (SELECT event_id FROM heads WHERE next_attempt_at <= now())
ORDER BY sequence_id
FOR UPDATE SKIP LOCKED
LIMIT :batch
```

Three details, each load-bearing:

- **``DISTINCT ON (partition_key) ... ORDER BY partition_key, sequence_id``** selects the head of
  every partition. The ``next_attempt_at`` filter is applied *after* — deliberately. Filtering
  inside the CTE would let a later row become the "head" while the true head sits in backoff,
  and publish it out of order. That is the ordering bug this query exists to prevent, and it is
  one line away from being reintroduced.

- **``FOR UPDATE SKIP LOCKED``** is what makes concurrent publishers safe. Two relays running the
  same query take disjoint rows: the second skips what the first has locked rather than blocking
  on it or, worse, reading it and publishing a duplicate. Without ``SKIP LOCKED`` the relays
  serialise; without ``FOR UPDATE`` they duplicate.

- **The lock is the claim.** There is no ``status = 'publishing'`` column, because a process that
  sets one and then dies leaves a row nothing reclaims — not pending, so no relay takes it; not
  published, so nothing completes it. A row lock is released by the database when the connection
  dies. That is crash recovery for free, and it is why the status enum has three values.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from cip_core.logging import get_logger
from cip_platform.outbox.models import OutboxEvent, OutboxStatus
from cip_platform.outbox.store import ClaimedBatch, OutboxStats

__all__ = ["PostgresOutboxStore", "append_to_outbox"]

_log = get_logger(__name__)

_COLUMNS = (
    "event_id, sequence_id, event_type, tenant_id, partition_key, aggregate_type, "
    "aggregate_id, correlation_id, traceparent, status, attempts, created_at, "
    "next_attempt_at, published_at, last_error, payload"
)

_CLAIM_SQL = text(f"""
WITH heads AS (
    SELECT DISTINCT ON (partition_key) event_id, next_attempt_at
    FROM outbox_events
    WHERE status = 'pending'
    ORDER BY partition_key, sequence_id
)
SELECT {_COLUMNS}
FROM outbox_events
WHERE event_id IN (SELECT event_id FROM heads WHERE next_attempt_at <= :now)
ORDER BY sequence_id
FOR UPDATE SKIP LOCKED
LIMIT :batch
""")

_APPEND_SQL = text("""
INSERT INTO outbox_events (
    event_id, event_type, tenant_id, partition_key, aggregate_type, aggregate_id,
    correlation_id, traceparent, status, attempts, created_at, next_attempt_at, payload
) VALUES (
    :event_id, :event_type, :tenant_id, :partition_key, :aggregate_type, :aggregate_id,
    :correlation_id, :traceparent, 'pending', 0, :created_at, :created_at, CAST(:payload AS JSONB)
)
ON CONFLICT (event_id) DO NOTHING
RETURNING sequence_id
""")


async def append_to_outbox(session: AsyncSession, event: OutboxEvent) -> OutboxEvent:
    """Write an event **inside the caller's transaction**.

    Takes a session rather than a manager, and never commits. If this committed, the event would
    land whether or not the business data did — reintroducing the dual-write from the other
    direction, where a consumer acts on something that was rolled back.

    ``ON CONFLICT DO NOTHING`` on ``event_id`` makes the append itself idempotent, so a retried
    application-level operation does not enqueue the same event twice.
    """
    result = await session.execute(
        _APPEND_SQL,
        {
            "event_id": str(event.event_id),
            "event_type": event.event_type,
            "tenant_id": str(event.tenant_id),
            "partition_key": event.partition_key,
            "aggregate_type": event.aggregate_type,
            "aggregate_id": event.aggregate_id,
            "correlation_id": event.correlation_id,
            "traceparent": event.traceparent,
            "created_at": event.created_at,
            "payload": json.dumps(event.payload, sort_keys=True),
        },
    )
    row = result.first()
    if row is None:
        return event  # already present; the id is the dedupe key and it did its job
    return OutboxEvent(**{**_as_kwargs(event), "sequence_id": int(row[0])})


def _as_kwargs(event: OutboxEvent) -> dict[str, Any]:
    return {
        "event_type": event.event_type,
        "tenant_id": event.tenant_id,
        "partition_key": event.partition_key,
        "payload": event.payload,
        "event_id": event.event_id,
        "aggregate_type": event.aggregate_type,
        "aggregate_id": event.aggregate_id,
        "correlation_id": event.correlation_id,
        "traceparent": event.traceparent,
        "status": event.status,
        "attempts": event.attempts,
        "created_at": event.created_at,
        "next_attempt_at": event.next_attempt_at,
        "published_at": event.published_at,
        "last_error": event.last_error,
    }


class PostgresOutboxStore:
    """The relay's view of the outbox.

    Holds a session *factory*, not a session: each claim/resolve cycle is its own transaction,
    because a relay that held one long transaction would pin a connection and hold row locks for
    the lifetime of the process.
    """

    def __init__(self, session_factory: Any) -> None:
        self._session_factory = session_factory

    def claim(self, limit: int, *, now: dt.datetime | None = None) -> _PostgresClaim:
        """Claim the due head of each partition. Use as ``async with store.claim(n) as batch``.

        The transaction stays open for the lifetime of the context, which is what makes the row
        lock meaningful: a relay that claimed, committed, then published would have released the
        lock *before* doing the work, and a second relay could claim and publish the same row.
        """
        return _PostgresClaim(self._session_factory, limit, now or dt.datetime.now(dt.UTC))

    async def stats(self, *, now: dt.datetime | None = None) -> OutboxStats:
        moment = now or dt.datetime.now(dt.UTC)
        async with self._session_factory() as session:
            result = await session.execute(
                text(
                    "SELECT status, count(*) AS n, min(created_at) AS oldest "
                    "FROM outbox_events GROUP BY status"
                )
            )
            counts = {row.status: (row.n, row.oldest) for row in result}
        pending, oldest = counts.get("pending", (0, None))
        return OutboxStats(
            pending=pending,
            published=counts.get("published", (0, None))[0],
            dead=counts.get("dead", (0, None))[0],
            oldest_pending_age_seconds=(moment - oldest).total_seconds() if oldest else 0.0,
        )

    async def replay(self, event_id: uuid.UUID) -> bool:
        async with self._session_factory() as session:
            result = await session.execute(
                text(
                    "UPDATE outbox_events SET status = 'pending', attempts = 0, "
                    "last_error = '', next_attempt_at = :now "
                    "WHERE event_id = :id AND status = 'dead'"
                ),
                {"now": dt.datetime.now(dt.UTC), "id": str(event_id)},
            )
            await session.commit()
            return bool(result.rowcount)


def _row_to_event(row: Any) -> OutboxEvent:
    payload = row["payload"]
    return OutboxEvent(
        event_id=uuid.UUID(str(row["event_id"])),
        sequence_id=int(row["sequence_id"]),
        event_type=row["event_type"],
        tenant_id=uuid.UUID(str(row["tenant_id"])),
        partition_key=row["partition_key"],
        aggregate_type=row["aggregate_type"] or "",
        aggregate_id=row["aggregate_id"] or "",
        correlation_id=row["correlation_id"] or "",
        traceparent=row["traceparent"] or "",
        status=OutboxStatus(row["status"]),
        attempts=int(row["attempts"]),
        created_at=row["created_at"],
        next_attempt_at=row["next_attempt_at"],
        published_at=row["published_at"],
        last_error=row["last_error"] or "",
        payload=payload if isinstance(payload, dict) else json.loads(payload or "{}"),
    )


class _PostgresClaim:
    """One claim: a transaction held open across the publish, closed exactly once."""

    def __init__(self, session_factory: Any, limit: int, now: dt.datetime) -> None:
        self._session_factory = session_factory
        self._limit = limit
        self._now = now
        self._session: Any = None
        self._batch = ClaimedBatch()

    async def __aenter__(self) -> ClaimedBatch:
        self._session = self._session_factory()
        await self._session.begin()
        try:
            # The relay is cross-tenant by nature — one process drains every tenant's events —
            # so it cannot set a single `app.tenant_id` and would see nothing under the tenant
            # isolation policy. This opts into the relay policy instead.
            #
            # SET LOCAL, so it lasts exactly this transaction. A session-scoped setting on a
            # pooled connection would leak relay privileges into whatever borrowed it next,
            # which is precisely the cross-tenant read the policy exists to prevent.
            #
            # **This is a guard against accidental cross-tenant reads, not a security boundary.**
            # The application role can set the same GUC. The real boundary is running the relay
            # under its own database role; see the migration and ADR-0041.
            await self._session.execute(text("SET LOCAL app.outbox_relay = 'on'"))
            result = await self._session.execute(
                _CLAIM_SQL, {"now": self._now, "batch": self._limit}
            )
            self._batch.events = [_row_to_event(row) for row in result.mappings()]
        except Exception:
            await self._session.rollback()
            await self._session.close()
            self._session = None
            raise
        return self._batch

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._session is None:
            return
        try:
            await self._apply()
            await self._session.commit()
        except Exception:
            # Rolling back releases every lock, so the batch is reclaimed on the next poll
            # rather than stranded. Nothing was marked published, so nothing is lost.
            await self._session.rollback()
            raise
        finally:
            await self._session.close()
            self._session = None

    async def _apply(self) -> None:
        session, batch = self._session, self._batch
        if batch.published:
            await session.execute(
                text(
                    "UPDATE outbox_events SET status = 'published', published_at = :now, "
                    "attempts = attempts + 1 WHERE event_id = ANY(:ids)"
                ),
                {
                    "now": dt.datetime.now(dt.UTC),
                    "ids": [str(e.event_id) for e in batch.published],
                },
            )
        for event, error, retry_in in batch.failed:
            await session.execute(
                text(
                    "UPDATE outbox_events SET attempts = attempts + 1, last_error = :error, "
                    "next_attempt_at = :next WHERE event_id = :id"
                ),
                {
                    "error": error[:500],
                    "next": dt.datetime.now(dt.UTC) + dt.timedelta(seconds=retry_in),
                    "id": str(event.event_id),
                },
            )
        for event, error in batch.dead:
            await session.execute(
                text(
                    "UPDATE outbox_events SET status = 'dead', attempts = attempts + 1, "
                    "last_error = :error WHERE event_id = :id"
                ),
                {"error": error[:500], "id": str(event.event_id)},
            )
        # Unresolved rows need no UPDATE: committing releases their locks and leaves them
        # pending, so the next poll picks them up without charging an attempt for a crash.
