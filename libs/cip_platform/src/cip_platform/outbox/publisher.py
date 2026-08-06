"""The relay: outbox rows to the broker.

Polls, publishes, retries, and dead-letters. It is the only component permitted to publish
platform events, because the guarantee is not "events reach Kafka" — it is "an event that was
committed with its business data reaches Kafka *exactly as many times as the consumer can
tolerate*". A second publishing path bypassing the outbox would make the guarantee untrue while
leaving every test passing.

**At-least-once, and the duplicate is bounded.** Three mechanisms stack:

1. The Kafka producer is idempotent (`enable_idempotence`), so a retry inside the client whose
   acknowledgement was lost does not duplicate at the broker.
2. The row is only marked published *after* the broker acknowledges, so a crash between the two
   redelivers rather than loses. That redelivery is the duplicate the design accepts.
3. Every message carries `eventId`, stable across every redelivery, so a consumer that has seen
   it discards the copy.

Removing any one of the three turns a bounded duplicate into either a lost event or an unbounded
one.

**Retry is not the same as failure.** A broker that is down is transient and the event must wait;
a payload that cannot be serialised will never succeed and retrying it forever blocks its whole
partition. The publisher separates them, and the separation is the reason a single poison message
cannot stall a tenant's entire stream.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from typing import Any

from cip_core.logging import get_logger
from cip_platform.outbox.models import OutboxEvent
from cip_platform.resilience.breaker import BreakerOpenError

__all__ = ["OutboxPublisher", "PublisherConfig", "PublisherStats"]

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class PublisherConfig:
    """How the relay behaves."""

    batch_size: int = 50
    poll_interval_seconds: float = 1.0
    """How long to wait when the outbox is empty. The latency cost of polling over CDC; one
    second is well inside what a clinical event stream needs and keeps database load trivial."""

    idle_backoff_max_seconds: float = 10.0
    """The poll interval grows toward this while the outbox stays empty. A relay polling every
    second forever costs one query per second per replica, all day, to find nothing."""

    max_attempts: int = 8
    """Attempts before an event is dead-lettered. With the backoff below this spans roughly ten
    minutes — long enough to ride out a broker restart, short enough that a genuinely broken
    event is visible while somebody is still looking."""

    base_retry_seconds: float = 1.0
    max_retry_seconds: float = 120.0
    dead_letter_suffix: str = ".dlq"

    def retry_delay(self, attempt: int) -> float:
        return min(self.base_retry_seconds * (2 ** max(0, attempt - 1)), self.max_retry_seconds)


@dataclass(slots=True)
class PublisherStats:
    """What the relay has done. Exposed to metrics and health."""

    polls: int = 0
    claimed: int = 0
    published: int = 0
    retried: int = 0
    dead_lettered: int = 0
    breaker_rejections: int = 0
    last_error: str = ""
    last_publish_at: float = 0.0
    total_publish_ms: float = 0.0

    @property
    def mean_publish_ms(self) -> float:
        return self.total_publish_ms / self.published if self.published else 0.0

    def to_json(self) -> dict[str, Any]:
        return {
            "polls": self.polls,
            "claimed": self.claimed,
            "published": self.published,
            "retried": self.retried,
            "deadLettered": self.dead_lettered,
            "breakerRejections": self.breaker_rejections,
            "meanPublishMs": round(self.mean_publish_ms, 3),
            "lastError": self.last_error,
        }


class OutboxPublisher:
    """Polls the outbox and publishes to the broker."""

    def __init__(
        self,
        store: Any,
        producer: Any,
        *,
        config: PublisherConfig | None = None,
        guard: Any = None,
        metrics: Any = None,
        clock: Any = time.monotonic,
    ) -> None:
        self._store = store
        self._producer = producer
        """Anything with ``async publish_message(topic, key, value)``. Not the ``EventBus``
        protocol: the relay publishes a pre-serialised outbox row rather than an ``Event``, and
        widening ``EventBus`` to carry both shapes would blur what it means."""
        self._config = config or PublisherConfig()
        self._guard = guard
        self._metrics = metrics
        self._clock = clock
        self._stats = PublisherStats()
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._idle_for = 0.0

    @property
    def stats(self) -> PublisherStats:
        return self._stats

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.is_running:
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._run(), name="outbox-publisher")
        _log.info("outbox.publisher_started", batch_size=self._config.batch_size)

    async def stop(self) -> None:
        """Signal and wait.

        Waits rather than cancelling, so a batch mid-publish finishes and commits its outcomes.
        Cancelling would roll the claim back — correct, but it would republish on restart for no
        reason.
        """
        self._stopping.set()
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        _log.info("outbox.publisher_stopped", **self._stats.to_json())

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                published = await self.drain_once()
            except Exception as exc:
                # The loop must not die. A relay that exits on an unexpected error stops
                # delivering every event on the platform, and the only symptom is a backlog
                # nobody is watching yet.
                self._stats.last_error = f"{type(exc).__name__}: {exc}"
                _log.error("outbox.poll_failed", error=type(exc).__name__, detail=str(exc)[:200])
                published = 0

            if published:
                self._idle_for = 0.0
                continue  # more work is likely waiting; poll again immediately

            # Grow the idle wait geometrically from the poll interval to the cap. A relay
            # polling every second forever costs one query per second per replica, all day, to
            # find nothing; any work resets it to zero above.
            next_wait = (
                self._config.poll_interval_seconds if self._idle_for == 0.0 else self._idle_for * 2
            )
            self._idle_for = min(next_wait, self._config.idle_backoff_max_seconds)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=self._idle_for)

    async def drain_once(self) -> int:
        """Claim one batch, publish it, record every outcome. Returns published count."""
        self._stats.polls += 1
        async with self._store.claim(self._config.batch_size) as batch:
            if not batch.events:
                return 0
            self._stats.claimed += len(batch.events)
            for event in batch.events:
                await self._publish_one(event, batch)
            return len(batch.published)

    async def _publish_one(self, event: OutboxEvent, batch: Any) -> None:
        began = time.perf_counter()
        try:
            await self._send(event)
        except BreakerOpenError as exc:
            # The circuit is open. Not a failure of *this* event, so it must not consume an
            # attempt — charging it would march every pending event toward the dead-letter queue
            # during an outage the events had nothing to do with. Retry when the circuit allows.
            self._stats.breaker_rejections += 1
            batch.record_failed(
                event,
                f"circuit open: {exc}",
                retry_in_seconds=max(exc.retry_after_seconds, self._config.base_retry_seconds),
            )
            return
        except Exception as exc:
            elapsed = (time.perf_counter() - began) * 1000
            detail = f"{type(exc).__name__}: {exc}"
            self._stats.last_error = detail
            if _is_permanent(exc) or event.attempts + 1 >= self._config.max_attempts:
                await self._dead_letter(event, detail, batch)
                return
            delay = self._config.retry_delay(event.attempts + 1)
            self._stats.retried += 1
            batch.record_failed(event, detail, retry_in_seconds=delay)
            _log.warning(
                "outbox.publish_retry",
                event_id=str(event.event_id),
                event_type=event.event_type,
                attempts=event.attempts + 1,
                retry_in_s=round(delay, 2),
                duration_ms=round(elapsed, 2),
                error=type(exc).__name__,
            )
            return

        elapsed = (time.perf_counter() - began) * 1000
        self._stats.published += 1
        self._stats.total_publish_ms += elapsed
        self._stats.last_publish_at = self._clock()
        batch.record_published(event)
        if self._metrics is not None:
            with contextlib.suppress(Exception):
                self._metrics.record_outbox_published(event_type=event.event_type)
        _log.info(
            "outbox.published",
            event_id=str(event.event_id),
            event_type=event.event_type,
            partition_key=event.partition_key,
            correlation_id=event.correlation_id,
            duration_ms=round(elapsed, 2),
        )

    async def _send(self, event: OutboxEvent) -> None:
        """Publish, through the guard when one is configured."""
        topic = _topic_for(event.event_type)

        async def operation() -> None:
            await self._producer.publish_message(
                topic=topic, key=event.partition_key, value=event.message()
            )

        if self._guard is None:
            await operation()
        else:
            await self._guard.call(operation, operation_name="publish")

    async def _dead_letter(self, event: OutboxEvent, error: str, batch: Any) -> None:
        """Move an exhausted or poison event aside.

        Published to the dead-letter topic *before* the row is marked dead, and the row is
        marked dead regardless of whether that publish succeeds. If the DLQ write is what failed,
        retrying the row forever would block its partition — which is the outcome the dead-letter
        queue exists to prevent. The row keeps its payload and its error, so a replay is
        available once the cause is understood.
        """
        with contextlib.suppress(Exception):
            await self._producer.publish_message(
                topic=_topic_for(event.event_type) + self._config.dead_letter_suffix,
                key=event.partition_key,
                value={**event.message(), "deadLetterReason": error[:500]},
            )
        self._stats.dead_lettered += 1
        batch.record_dead(event, error)
        _log.error(
            "outbox.dead_lettered",
            event_id=str(event.event_id),
            event_type=event.event_type,
            partition_key=event.partition_key,
            attempts=event.attempts + 1,
            error=error[:200],
        )

    async def health(self) -> dict[str, Any]:
        stats = await self._store.stats()
        return {
            "running": self.is_running,
            "publisher": self._stats.to_json(),
            "outbox": stats.to_json(),
        }


#: Errors that will never succeed on retry.
#:
#: Deliberately narrow. Classifying an unknown error as permanent discards an event that a retry
#: would have delivered, so anything unrecognised is treated as transient and bounded by
#: ``max_attempts`` instead. The cost of guessing wrong in this direction is delay; in the other
#: it is data loss.
_PERMANENT = (TypeError, ValueError, UnicodeDecodeError)


def _is_permanent(exc: BaseException) -> bool:
    return isinstance(exc, _PERMANENT)


def _topic_for(event_type: str) -> str:
    return f"cip.{event_type.replace('.', '-')}"


@dataclass(slots=True)
class _NullMetrics:
    """Used when no metrics registry is supplied."""

    recorded: list[str] = field(default_factory=list)

    def record_outbox_published(self, *, event_type: str) -> None:
        self.recorded.append(event_type)
