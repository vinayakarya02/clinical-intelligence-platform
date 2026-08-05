"""Choosing a task-queue backend from settings.

The counterpart to :mod:`cip_platform.cache.factory`, and it exists for the same reason: the
configuration surface named a backend that no code path could build.
"""

from __future__ import annotations

from cip_platform.cache.factory import build_redis_client
from cip_platform.config import QueuePolicy
from cip_platform.tasks.base import TaskQueue
from cip_platform.tasks.memory import InMemoryTaskQueue
from cip_platform.tasks.redis_queue import RedisTaskQueue

__all__ = ["QueueBackendError", "build_task_queue"]


class QueueBackendError(RuntimeError):
    """The configured queue backend could not be built."""


def build_task_queue(policy: QueuePolicy) -> TaskQueue:
    """The queue this configuration asks for.

    Raises rather than falling back to memory. An in-memory queue executes inline and loses
    everything on restart; a platform that silently degraded to it would report every job as
    complete and lose the work, which is worse than refusing to start.
    """
    if policy.backend == "memory":
        return InMemoryTaskQueue()
    if policy.backend == "redis":
        if not policy.broker_url.strip():
            raise QueueBackendError("queue backend is 'redis' but no broker_url is configured")
        return RedisTaskQueue(
            build_redis_client(policy.broker_url),
            visibility_timeout_seconds=policy.visibility_timeout_seconds,
        )
    raise QueueBackendError(f"unknown queue backend {policy.backend!r}")
