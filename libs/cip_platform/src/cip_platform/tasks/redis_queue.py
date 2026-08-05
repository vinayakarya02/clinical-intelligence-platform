"""A durable task queue on Redis.

Replaces the ``celery`` backend that configuration named and no code implemented
(``docs/design/adr-0040-redis-task-queue.md``). Celery would have brought RabbitMQ, a separate
worker runtime, and a result backend — three operational systems for three job kinds — while
Redis is already required for the cache and, from W5, for cluster-wide rate limiting.

The delivery guarantee is **at-least-once**, and that is a deliberate choice rather than a
limitation: exactly-once across a queue and a database requires distributed transactions, and
every practical system instead makes the *work* idempotent. ``TaskSpec.idempotency_key`` exists
for exactly this, and its docstring already says so — "at-least-once delivery guarantees this
task will run twice eventually."

Three structures per queue:

- ``cip:q:{queue}`` — a sorted set, scored by ``(priority, scheduled_for)``. A sorted set rather
  than a list because the queue must honour both priority and delayed scheduling, and a list
  gives neither.
- ``cip:claimed:{queue}`` — a sorted set scored by claim deadline. A job whose deadline passes
  is redelivered by :meth:`reclaim_expired`, which is what makes a worker crash recoverable
  rather than a silent loss.
- ``cip:result:{task_id}`` — the terminal outcome, TTL-bounded.

Claiming is atomic via a Lua script. Doing it with ``ZRANGE`` then ``ZREM`` from Python lets two
workers read the same member before either removes it, and both then run the job — which is the
one failure this design exists to prevent, because it is invisible under light load and appears
only under the concurrency that production has.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import Any

from cip_core.logging import get_logger
from cip_platform.tasks.base import (
    JobKind,
    TaskHandler,
    TaskResult,
    TaskSpec,
    TaskStatus,
)

__all__ = ["RedisTaskQueue"]

_log = get_logger(__name__)

_QUEUE_KEY = "cip:q:{queue}"
_CLAIMED_KEY = "cip:claimed:{queue}"
_RESULT_KEY = "cip:result:{task_id}"
_PAYLOAD_KEY = "cip:task:{task_id}"

#: Claim the highest-priority due job and move it to the claimed set, atomically.
#:
#: KEYS[1] ready sorted set · KEYS[2] claimed sorted set
#: ARGV[1] now (epoch seconds) · ARGV[2] claim deadline (epoch seconds)
_CLAIM_SCRIPT = """
local due = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', ARGV[1], 'LIMIT', 0, 1)
if #due == 0 then return nil end
local member = due[1]
redis.call('ZREM', KEYS[1], member)
redis.call('ZADD', KEYS[2], ARGV[2], member)
return member
"""

#: Move every claim whose deadline has passed back to the ready set.
#:
#: KEYS[1] claimed sorted set · KEYS[2] ready sorted set · ARGV[1] now
_RECLAIM_SCRIPT = """
local expired = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
for _, member in ipairs(expired) do
  redis.call('ZREM', KEYS[1], member)
  redis.call('ZADD', KEYS[2], 0, member)
end
return #expired
"""


def _score(spec: TaskSpec, *, now: dt.datetime) -> float:
    """Ordering: priority first, then scheduled time.

    Priority is the major component because a priority-9 job scheduled for now must not overtake
    a priority-1 job scheduled a second later. Multiplying priority by a value larger than any
    plausible delay keeps the two from interleaving.
    """
    when = spec.scheduled_for or now
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.UTC)
    return float(spec.priority) * 1e12 + when.timestamp()


class RedisTaskQueue:
    """A ``TaskQueue`` backed by Redis.

    Takes a client rather than a URL, for the same reason ``RedisCache`` does: pooling and TLS
    belong to whatever builds the client.
    """

    def __init__(
        self,
        client: Any,
        *,
        visibility_timeout_seconds: int = 900,
        result_ttl_seconds: int = 24 * 3600,
        clock: Any = None,
    ) -> None:
        if visibility_timeout_seconds < 1:
            raise ValueError("visibility_timeout_seconds must be >= 1")
        self._client = client
        self._visibility = visibility_timeout_seconds
        self._result_ttl = result_ttl_seconds
        self._clock = clock or (lambda: dt.datetime.now(dt.UTC))
        self._handlers: dict[JobKind, TaskHandler] = {}
        self._claim = client.register_script(_CLAIM_SCRIPT)
        self._reclaim = client.register_script(_RECLAIM_SCRIPT)

    def register(self, kind: JobKind, handler: TaskHandler) -> None:
        self._handlers[kind] = handler

    @property
    def registered(self) -> frozenset[JobKind]:
        return frozenset(self._handlers)

    async def enqueue(self, spec: TaskSpec) -> uuid.UUID:
        """Persist the job and make it visible to workers.

        The payload is written *before* the queue entry. Reversing the order creates a window in
        which a worker claims an id whose payload does not exist yet — rare, load-dependent, and
        indistinguishable from data corruption when it happens.
        """
        now = self._clock()
        payload = json.dumps(_encode(spec), sort_keys=True)
        await self._client.set(_PAYLOAD_KEY.format(task_id=spec.task_id), payload)
        await self._client.zadd(
            _QUEUE_KEY.format(queue=spec.queue), {str(spec.task_id): _score(spec, now=now)}
        )
        _log.info(
            "queue.enqueued",
            task_id=str(spec.task_id),
            kind=str(spec.kind),
            queue=spec.queue,
            priority=spec.priority,
        )
        return spec.task_id

    async def claim(self, queue: str) -> TaskSpec | None:
        """Take the next due job, or None. Atomic — see the module docstring."""
        now = self._clock()
        member = await self._claim(
            keys=[_QUEUE_KEY.format(queue=queue), _CLAIMED_KEY.format(queue=queue)],
            args=[_score_now(now), now.timestamp() + self._visibility],
        )
        if member is None:
            return None
        task_id = member.decode() if isinstance(member, bytes) else str(member)
        raw = await self._client.get(_PAYLOAD_KEY.format(task_id=task_id))
        if raw is None:
            # The payload expired or was never written. Drop the claim rather than retrying
            # forever against an id with no work behind it.
            await self._client.zrem(_CLAIMED_KEY.format(queue=queue), task_id)
            _log.warning("queue.payload_missing", task_id=task_id)
            return None
        return _decode(json.loads(raw))

    async def acknowledge(self, spec: TaskSpec, result: TaskResult) -> None:
        """Record the outcome and release the claim."""
        await self._client.zrem(_CLAIMED_KEY.format(queue=spec.queue), str(spec.task_id))
        await self._client.set(
            _RESULT_KEY.format(task_id=spec.task_id),
            json.dumps(_encode_result(result), sort_keys=True),
            ex=self._result_ttl,
        )
        await self._client.delete(_PAYLOAD_KEY.format(task_id=spec.task_id))

    async def requeue(self, spec: TaskSpec, *, delay_seconds: float) -> None:
        """Return a failed job to the queue after a backoff."""
        now = self._clock()
        await self._client.zrem(_CLAIMED_KEY.format(queue=spec.queue), str(spec.task_id))
        when = now + dt.timedelta(seconds=delay_seconds)
        await self._client.zadd(
            _QUEUE_KEY.format(queue=spec.queue),
            {str(spec.task_id): float(spec.priority) * 1e12 + when.timestamp()},
        )

    async def reclaim_expired(self, queue: str) -> int:
        """Return every timed-out claim to the ready set.

        This is what makes a worker crash recoverable. Without it a job claimed by a process
        that dies is claimed forever, and the only symptom is that some work never completes.
        """
        now = self._clock()
        count = await self._reclaim(
            keys=[_CLAIMED_KEY.format(queue=queue), _QUEUE_KEY.format(queue=queue)],
            args=[now.timestamp()],
        )
        if count:
            _log.warning("queue.reclaimed", queue=queue, count=int(count))
        return int(count)

    async def result(self, task_id: uuid.UUID) -> TaskResult | None:
        raw = await self._client.get(_RESULT_KEY.format(task_id=task_id))
        if raw is None:
            return None
        return _decode_result(json.loads(raw))

    async def depth(self, queue: str) -> int:
        """Ready jobs. Excludes claimed ones, which are in flight rather than waiting."""
        return int(await self._client.zcard(_QUEUE_KEY.format(queue=queue)))


def _score_now(now: dt.datetime) -> float:
    """The upper bound for "due now" across every priority band."""
    return 9.0 * 1e12 + now.timestamp()


def _encode(spec: TaskSpec) -> dict[str, Any]:
    return {
        "kind": str(spec.kind),
        "tenant_id": str(spec.tenant_id),
        "payload": spec.payload,
        "task_id": str(spec.task_id),
        "idempotency_key": spec.idempotency_key,
        "correlation_id": spec.correlation_id,
        "traceparent": spec.traceparent,
        "max_retries": spec.max_retries,
        "priority": spec.priority,
        "scheduled_for": spec.scheduled_for.isoformat() if spec.scheduled_for else None,
    }


def _decode(data: dict[str, Any]) -> TaskSpec:
    scheduled = data.get("scheduled_for")
    return TaskSpec(
        kind=JobKind(data["kind"]),
        tenant_id=uuid.UUID(data["tenant_id"]),
        payload=data.get("payload") or {},
        task_id=uuid.UUID(data["task_id"]),
        idempotency_key=data.get("idempotency_key", ""),
        correlation_id=data.get("correlation_id", ""),
        traceparent=data.get("traceparent", ""),
        max_retries=data.get("max_retries", 3),
        priority=data.get("priority", 5),
        scheduled_for=dt.datetime.fromisoformat(scheduled) if scheduled else None,
    )


def _encode_result(result: TaskResult) -> dict[str, Any]:
    return {
        "task_id": str(result.task_id),
        "status": str(result.status),
        "attempts": result.attempts,
        "duration_ms": result.duration_ms,
        "error": result.error,
        "output": result.output,
    }


def _decode_result(data: dict[str, Any]) -> TaskResult:
    return TaskResult(
        task_id=uuid.UUID(data["task_id"]),
        status=TaskStatus(data["status"]),
        attempts=data.get("attempts", 1),
        duration_ms=data.get("duration_ms", 0.0),
        error=data.get("error"),
        output=data.get("output") or {},
    )
