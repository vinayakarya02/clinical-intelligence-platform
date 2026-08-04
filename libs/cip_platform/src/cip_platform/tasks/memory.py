"""In-process task queue.

Executes inline with the real retry, classification, idempotency, and dead-letter semantics,
so a job's failure handling is exercised in CI rather than only against a broker. Refused in
deployed environments — inline execution loses queued work on restart, and a queue that
forgets is worse than no queue because callers believe the work is scheduled.
"""

from __future__ import annotations

import time
import uuid
from collections import OrderedDict, deque

from cip_core.logging import get_logger
from cip_platform.tasks.base import (
    JobKind,
    PermanentTaskError,
    TaskHandler,
    TaskResult,
    TaskSpec,
    TaskStatus,
    TransientTaskError,
    backoff_seconds,
)

__all__ = ["InMemoryTaskQueue"]

_log = get_logger(__name__)


class InMemoryTaskQueue:
    """Runs tasks immediately, with production retry and idempotency behaviour."""

    def __init__(self, *, sleep: object = None, history_limit: int = 5000) -> None:
        self._handlers: dict[JobKind, TaskHandler] = {}
        # All three are bounded. This queue backs development as well as tests, so it runs in
        # a long-lived process where an unbounded results map, dedupe set, and dead-letter
        # list each grow one entry per task forever.
        #
        # The dedupe set is the subtle one: evicting an idempotency key means a *very* old
        # redelivery could run twice. That is the right trade — the alternative is unbounded
        # memory, and a broker will not redeliver a message thousands of tasks later.
        self._results: OrderedDict[uuid.UUID, TaskResult] = OrderedDict()
        self._completed: OrderedDict[str, None] = OrderedDict()
        self._dead_letters: deque[tuple[TaskSpec, str]] = deque(maxlen=history_limit)
        self._history_limit = history_limit
        self._sleep = sleep
        """Injectable so backoff is exercised without actually waiting. A test that sleeps for
        real backoff intervals is a test that gets deleted."""

    def register(self, kind: JobKind, handler: TaskHandler) -> None:
        if kind in self._handlers:
            raise ValueError(f"A handler for {kind} is already registered")
        self._handlers[kind] = handler

    async def enqueue(self, spec: TaskSpec) -> uuid.UUID:
        """Execute ``spec`` now, honouring idempotency and retries."""
        dedupe = spec.dedupe_key()
        if spec.idempotency_key and dedupe in self._completed:
            # At-least-once delivery means this *will* happen. Recording the skip rather than
            # silently returning makes a redelivery storm visible.
            _log.info("tasks.duplicate_skipped", kind=str(spec.kind), dedupe_key=dedupe)
            self._results[spec.task_id] = TaskResult(
                task_id=spec.task_id, status=TaskStatus.SUCCEEDED, attempts=0
            )
            return spec.task_id

        handler = self._handlers.get(spec.kind)
        if handler is None:
            # An unroutable task is a wiring bug, not a job failure: it would never succeed on
            # any worker, so it is dead-lettered immediately rather than retried.
            self._dead_letter(spec, f"no handler registered for {spec.kind}")
            return spec.task_id

        started = time.perf_counter()
        attempts = 0
        last_error = ""

        while attempts <= spec.max_retries:
            attempts += 1
            try:
                output = await handler.run(spec)
            except PermanentTaskError as exc:
                # Retrying cannot fix a malformed payload; three attempts would be three
                # identical failures and a delayed error.
                last_error = f"{type(exc).__name__}: {exc}"
                self._dead_letter(spec, last_error, attempts=attempts)
                return spec.task_id
            except TransientTaskError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempts > spec.max_retries:
                    break
                delay = backoff_seconds(attempts)
                _log.warning(
                    "tasks.retrying",
                    kind=str(spec.kind),
                    attempt=attempts,
                    delay_seconds=delay,
                    error=type(exc).__name__,
                )
                if callable(self._sleep):
                    await self._sleep(delay)  # type: ignore[misc]
                continue
            except Exception as exc:
                # An unclassified exception is treated as transient: most are, and the retry
                # cap bounds the cost of being wrong. The same reasoning as Phase 2's
                # embedding retry.
                last_error = f"{type(exc).__name__}: {exc}"
                if attempts > spec.max_retries:
                    break
                continue
            else:
                duration = (time.perf_counter() - started) * 1000
                if spec.idempotency_key:
                    self._completed[dedupe] = None
                    self._trim(self._completed)
                self._results[spec.task_id] = TaskResult(
                    task_id=spec.task_id,
                    status=TaskStatus.SUCCEEDED,
                    attempts=attempts,
                    duration_ms=duration,
                    output=output or {},
                )
                self._trim(self._results)
                _log.debug(
                    "tasks.succeeded",
                    kind=str(spec.kind),
                    attempts=attempts,
                    duration_ms=round(duration, 2),
                )
                return spec.task_id

        self._dead_letter(spec, last_error, attempts=attempts)
        return spec.task_id

    def _dead_letter(self, spec: TaskSpec, error: str, *, attempts: int = 1) -> None:
        """Record a task that will not be retried again.

        A dead-lettered task is *visible*. A job that exhausts its retries and vanishes is
        indistinguishable from one that was never enqueued, and the first person to notice is
        whoever asks why a tenant's documents are not searchable.
        """
        self._dead_letters.append((spec, error))
        self._results[spec.task_id] = TaskResult(
            task_id=spec.task_id,
            status=TaskStatus.DEAD_LETTERED,
            attempts=attempts,
            error=error,
        )
        self._trim(self._results)
        _log.error(
            "tasks.dead_lettered",
            kind=str(spec.kind),
            tenant=str(spec.tenant_id),
            attempts=attempts,
            error=error,
        )

    def _trim(self, mapping: OrderedDict) -> None:  # type: ignore[type-arg]
        """Drop oldest entries past the history limit."""
        while len(mapping) > self._history_limit:
            mapping.popitem(last=False)

    async def result(self, task_id: uuid.UUID) -> TaskResult | None:
        return self._results.get(task_id)

    def dead_letters(self) -> list[tuple[TaskSpec, str]]:
        return list(self._dead_letters)

    def registered_kinds(self) -> tuple[JobKind, ...]:
        return tuple(sorted(self._handlers, key=lambda k: k.value))
