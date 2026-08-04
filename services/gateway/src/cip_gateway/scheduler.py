"""Periodic work.

Schedules are declared as data so the set of things that run automatically is reviewable in
one place, rather than distributed across decorators.

Exactly one scheduler replica runs (ADR-0017). Two would enqueue every job twice, and the
deduplication that protects against redelivery would not help: two schedulers produce two
*different* idempotency keys for the same intended run unless the key is derived from the
schedule window, which is why it is.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from dataclasses import dataclass

from cip_core.logging import get_logger
from cip_platform.tasks.base import JobKind, TaskQueue, TaskSpec

__all__ = ["DEFAULT_SCHEDULES", "Schedule", "SchedulerService"]

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Schedule:
    """One recurring job."""

    name: str
    kind: JobKind
    interval_seconds: int
    payload: dict[str, object]
    description: str = ""

    def __post_init__(self) -> None:
        if self.interval_seconds < 60:
            # Anything wanting sub-minute cadence is not periodic maintenance; it is a
            # consumer, and should be one.
            raise ValueError("interval_seconds must be >= 60")

    def window_key(self, now: dt.datetime) -> str:
        """A key that is stable within one interval window.

        Derived from the window rather than from the wall clock, so two schedulers — or one
        scheduler restarted mid-window — produce the *same* key and the queue deduplicates
        them into one run.
        """
        bucket = int(now.timestamp()) // self.interval_seconds
        return f"schedule:{self.name}:{bucket}"


#: What runs automatically. Reviewable in one place.
DEFAULT_SCHEDULES: tuple[Schedule, ...] = (
    Schedule(
        name="audit-chain-verification",
        kind=JobKind.MAINTENANCE,
        interval_seconds=3600,
        payload={"task": "verify_audit_chain"},
        description="Hourly hash-chain verification of the audit log (Phase 1).",
    ),
    Schedule(
        name="nightly-evaluation",
        kind=JobKind.EVALUATION,
        interval_seconds=86_400,
        payload={"experiment": "nightly-regression"},
        description="Runs the labelled eval set and records it against the deployed versions.",
    ),
    Schedule(
        name="cache-stats",
        kind=JobKind.MAINTENANCE,
        interval_seconds=300,
        payload={"task": "report_cache_stats"},
        description="Publishes cache hit rates; a silently-broken cache looks like a slow one.",
    ),
)


class SchedulerService:
    """Enqueues scheduled work on its interval."""

    def __init__(
        self,
        queue: TaskQueue,
        *,
        tenant_id: uuid.UUID,
        schedules: tuple[Schedule, ...] = DEFAULT_SCHEDULES,
        clock: object = None,
    ) -> None:
        self._queue = queue
        self._tenant_id = tenant_id
        self._schedules = schedules
        self._clock = clock or (lambda: dt.datetime.now(dt.UTC))
        self._last_run: dict[str, int] = {}

    async def tick(self) -> list[str]:
        """Enqueue anything due. Returns the names enqueued.

        Separated from the run loop so a test can advance a clock and assert, rather than
        waiting for real intervals.
        """
        now = self._clock()  # type: ignore[operator]
        enqueued: list[str] = []

        for schedule in self._schedules:
            bucket = int(now.timestamp()) // schedule.interval_seconds
            if self._last_run.get(schedule.name) == bucket:
                continue
            self._last_run[schedule.name] = bucket
            await self._queue.enqueue(
                TaskSpec(
                    kind=schedule.kind,
                    tenant_id=self._tenant_id,
                    payload=dict(schedule.payload),
                    idempotency_key=schedule.window_key(now),
                    correlation_id=f"scheduler-{schedule.name}-{bucket}",
                )
            )
            enqueued.append(schedule.name)

        if enqueued:
            _log.info("scheduler.enqueued", schedules=enqueued)
        return enqueued

    async def run_forever(self, *, poll_seconds: int = 30) -> None:  # pragma: no cover - loop
        """Tick until cancelled."""
        while True:
            try:
                await self.tick()
            except Exception as exc:
                # A scheduler that dies stops all periodic work silently. Log and continue.
                _log.error("scheduler.tick_failed", error=type(exc).__name__)
            await asyncio.sleep(poll_seconds)


def main() -> None:  # pragma: no cover - process entrypoint
    """Scheduler entrypoint (``entrypoint.sh scheduler``)."""
    raise SystemExit(
        "The scheduler entrypoint requires a configured queue and tenant; wire it in the "
        "composition root before running this image with the 'scheduler' role."
    )


if __name__ == "__main__":  # pragma: no cover
    main()
