"""The outbox store: the contract, and the in-memory implementation.

**The ordering invariant, stated once and implemented identically in both stores:**

> Only the **head** of a partition is eligible for publication — the unpublished row with the
> lowest ``sequence_id`` for that ``partition_key`` — and only when its backoff has elapsed.

Without it, ordering breaks in a way that is easy to miss. Suppose events A and B share a
partition and A fails transiently. A goes into backoff; B is pending and due. A publisher that
selects "every due row" publishes B, and the consumer sees B before A. For a clinical stream
that is an admission processed after its own discharge.

The invariant costs throughput per partition and buys correctness: a partition proceeds in
order or not at all. Throughput comes from having many partitions — the key is the patient or
tenant, so parallelism scales with the workload rather than with the batch size.

The in-memory store is not a toy. It is the test double *and* the reference implementation of
the invariant: the Postgres store expresses the same rule in SQL, and a test that passes here
and fails there means the SQL is wrong.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field, replace
from typing import Any, Protocol, runtime_checkable

from cip_core.logging import get_logger
from cip_platform.outbox.models import OutboxEvent, OutboxStatus, PublishAttempt

__all__ = ["ClaimedBatch", "InMemoryOutboxStore", "OutboxStats", "OutboxStore"]

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class OutboxStats:
    """What an operator needs to know about the backlog."""

    pending: int = 0
    published: int = 0
    dead: int = 0
    oldest_pending_age_seconds: float = 0.0
    """The number that matters. A pending count of 40 is meaningless on its own — 40 rows a
    second old is healthy throughput, 40 rows an hour old is a stalled relay."""

    def to_json(self) -> dict[str, Any]:
        return {
            "pending": self.pending,
            "published": self.published,
            "dead": self.dead,
            "oldestPendingAgeSeconds": round(self.oldest_pending_age_seconds, 3),
        }


@dataclass(slots=True)
class ClaimedBatch:
    """Events claimed for publication, and the outcomes recorded against them.

    A batch rather than a bare list, and a context manager rather than a pair of calls, because
    the claim is held by a **transaction that must be closed exactly once**. An earlier draft
    stashed the session on the store instance and offered `claim()` / `resolve()` as separate
    methods; two concurrent relays sharing one store would then have overwritten each other's
    session, and the second `resolve` would have committed against the first's transaction. The
    context manager makes that shape unrepresentable.
    """

    events: list[OutboxEvent] = field(default_factory=list)
    published: list[OutboxEvent] = field(default_factory=list)
    failed: list[tuple[OutboxEvent, str, float]] = field(default_factory=list)
    dead: list[tuple[OutboxEvent, str]] = field(default_factory=list)

    def record_published(self, event: OutboxEvent) -> None:
        self.published.append(event)

    def record_failed(self, event: OutboxEvent, error: str, *, retry_in_seconds: float) -> None:
        self.failed.append((event, error, retry_in_seconds))

    def record_dead(self, event: OutboxEvent, error: str) -> None:
        self.dead.append((event, error))

    @property
    def unresolved(self) -> list[OutboxEvent]:
        """Claimed events with no outcome recorded.

        A publisher that raises partway leaves these behind. They must be released rather than
        silently dropped, or their partition stalls until the lock times out.
        """
        settled = {e.event_id for e in self.published}
        settled |= {e.event_id for e, _, _ in self.failed}
        settled |= {e.event_id for e, _ in self.dead}
        return [event for event in self.events if event.event_id not in settled]


@runtime_checkable
class OutboxStore(Protocol):
    """Where outbox events live.

    ``append`` is deliberately **not** on this protocol as a standalone operation for the
    Postgres store: an append that opens its own transaction defeats the entire pattern, because
    the event would commit separately from the business data it describes. The Postgres store
    takes the caller's session; this protocol describes what the *relay* needs.
    """

    async def claim(self, limit: int, *, now: dt.datetime | None = None) -> list[OutboxEvent]: ...

    async def mark_published(self, event: OutboxEvent, *, duration_ms: float = 0.0) -> None: ...

    async def mark_failed(
        self, event: OutboxEvent, error: str, *, retry_in_seconds: float, duration_ms: float = 0.0
    ) -> None: ...

    async def mark_dead(self, event: OutboxEvent, error: str) -> None: ...

    async def stats(self, *, now: dt.datetime | None = None) -> OutboxStats: ...


@dataclass(slots=True)
class InMemoryOutboxStore:
    """An outbox in a dict. The test double and the reference for the ordering invariant."""

    _rows: dict[uuid.UUID, OutboxEvent] = field(default_factory=dict, init=False)
    _history: dict[uuid.UUID, list[PublishAttempt]] = field(default_factory=dict, init=False)
    _sequence: int = field(default=0, init=False)
    _claimed: set[uuid.UUID] = field(default_factory=set, init=False)
    """Stands in for the row lock. A claimed row is invisible to another claim until it is
    resolved, which is what ``FOR UPDATE SKIP LOCKED`` gives the Postgres store."""

    async def append(self, event: OutboxEvent) -> OutboxEvent:
        """Add an event, assigning its sequence.

        Rejects a duplicate ``event_id`` rather than overwriting: the id is the deduplication
        key, and silently replacing a row under an id a consumer may already have seen would
        make the same id mean two different things.
        """
        if event.event_id in self._rows:
            raise ValueError(f"event {event.event_id} is already in the outbox")
        self._sequence += 1
        stored = replace(event, sequence_id=self._sequence)
        self._rows[stored.event_id] = stored
        return stored

    def claim(self, limit: int, *, now: dt.datetime | None = None) -> Any:
        """Claim the due head of each partition. Use as ``async with store.claim(n) as batch``."""
        return _InMemoryClaim(self, limit, now)

    async def _select(self, limit: int, now: dt.datetime | None) -> list[OutboxEvent]:
        """The due head of each partition, oldest first.

        See the module docstring for why it is the head and not simply "everything due".
        """
        moment = now or dt.datetime.now(dt.UTC)
        heads: dict[str, OutboxEvent] = {}
        for row in self._rows.values():
            if row.status is not OutboxStatus.PENDING or row.event_id in self._claimed:
                continue
            current = heads.get(row.partition_key)
            if current is None or row.sequence_id < current.sequence_id:
                heads[row.partition_key] = row

        due = [row for row in heads.values() if row.next_attempt_at <= moment]
        due.sort(key=lambda row: row.sequence_id)
        selected = due[:limit]
        self._claimed.update(row.event_id for row in selected)
        return selected

    async def mark_published(self, event: OutboxEvent, *, duration_ms: float = 0.0) -> None:
        self._claimed.discard(event.event_id)
        stored = self._rows.get(event.event_id)
        if stored is None:
            return
        self._rows[event.event_id] = replace(
            stored,
            status=OutboxStatus.PUBLISHED,
            published_at=dt.datetime.now(dt.UTC),
            attempts=stored.attempts + 1,
        )
        self._record(event, PublishAttempt(dt.datetime.now(dt.UTC), True, "", duration_ms))

    async def mark_failed(
        self,
        event: OutboxEvent,
        error: str,
        *,
        retry_in_seconds: float,
        duration_ms: float = 0.0,
    ) -> None:
        self._claimed.discard(event.event_id)
        stored = self._rows.get(event.event_id)
        if stored is None:
            return
        self._rows[event.event_id] = replace(
            stored,
            attempts=stored.attempts + 1,
            last_error=error[:500],
            next_attempt_at=dt.datetime.now(dt.UTC) + dt.timedelta(seconds=retry_in_seconds),
        )
        self._record(event, PublishAttempt(dt.datetime.now(dt.UTC), False, error, duration_ms))

    async def mark_dead(self, event: OutboxEvent, error: str) -> None:
        self._claimed.discard(event.event_id)
        stored = self._rows.get(event.event_id)
        if stored is None:
            return
        self._rows[event.event_id] = replace(
            stored,
            status=OutboxStatus.DEAD,
            attempts=stored.attempts + 1,
            last_error=error[:500],
        )

    async def replay(self, event_id: uuid.UUID) -> bool:
        """Return a dead event to pending.

        Attempts are reset. A replayed event that kept its exhausted count would die again on
        its first transient failure, which defeats the point of replaying it after the cause has
        been fixed.
        """
        stored = self._rows.get(event_id)
        if stored is None or stored.status is not OutboxStatus.DEAD:
            return False
        self._rows[event_id] = replace(
            stored,
            status=OutboxStatus.PENDING,
            attempts=0,
            next_attempt_at=dt.datetime.now(dt.UTC),
            last_error="",
        )
        return True

    async def stats(self, *, now: dt.datetime | None = None) -> OutboxStats:
        moment = now or dt.datetime.now(dt.UTC)
        pending = [r for r in self._rows.values() if r.status is OutboxStatus.PENDING]
        oldest = min((r.created_at for r in pending), default=moment)
        return OutboxStats(
            pending=len(pending),
            published=sum(1 for r in self._rows.values() if r.status is OutboxStatus.PUBLISHED),
            dead=sum(1 for r in self._rows.values() if r.status is OutboxStatus.DEAD),
            oldest_pending_age_seconds=(moment - oldest).total_seconds() if pending else 0.0,
        )

    def history(self, event_id: uuid.UUID) -> tuple[PublishAttempt, ...]:
        return tuple(self._history.get(event_id, ()))

    def get(self, event_id: uuid.UUID) -> OutboxEvent | None:
        return self._rows.get(event_id)

    def all_events(self) -> tuple[OutboxEvent, ...]:
        return tuple(sorted(self._rows.values(), key=lambda row: row.sequence_id))

    def _record(self, event: OutboxEvent, attempt: PublishAttempt) -> None:
        # Bounded: an event retrying for hours would otherwise accumulate history without limit,
        # and the last few attempts are the ones anybody reads.
        history = self._history.setdefault(event.event_id, [])
        history.append(attempt)
        if len(history) > 20:
            del history[:-20]


class _InMemoryClaim:
    """The in-memory analogue of a held transaction.

    Mirrors the Postgres store's shape exactly so the publisher is one implementation. On exit
    it applies the recorded outcomes and releases every claim — including any left unresolved by
    a publisher that raised, which would otherwise stall their partitions forever.
    """

    def __init__(self, store: InMemoryOutboxStore, limit: int, now: dt.datetime | None) -> None:
        self._store = store
        self._limit = limit
        self._now = now
        self._batch = ClaimedBatch()

    async def __aenter__(self) -> ClaimedBatch:
        self._batch.events = await self._store._select(self._limit, self._now)
        return self._batch

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        for event in self._batch.published:
            await self._store.mark_published(event)
        for event, error, retry_in in self._batch.failed:
            await self._store.mark_failed(event, error, retry_in_seconds=retry_in)
        for event, error in self._batch.dead:
            await self._store.mark_dead(event, error)
        for event in self._batch.unresolved:
            # Released without counting an attempt: nothing was tried, so charging the event an
            # attempt would march it toward the dead-letter queue for a publisher's crash.
            self._store._claimed.discard(event.event_id)
