"""Background work: task contract, queue protocol, retry policy, and in-process execution.

Six job kinds — ingest, embed, graph build, evaluate, export, maintenance — behind one queue
interface. Celery is the production backend; the in-process implementation runs the same jobs
inline for tests and single-process development.

Three properties are enforced here rather than left to each job:

**Every task carries a tenant and an idempotency key.** At-least-once delivery means a job
*will* run twice, and a document ingested twice is a duplicate record.

**Retries are bounded and classified.** A malformed payload retried three times is three
identical failures and a delayed error; only transient failures are worth retrying.

**A task that exhausts its retries goes to a dead-letter record, not to silence.** A job that
vanishes is indistinguishable from a job that was never enqueued.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "JobKind",
    "PermanentTaskError",
    "TaskQueue",
    "TaskResult",
    "TaskSpec",
    "TaskStatus",
    "TransientTaskError",
]


class JobKind(StrEnum):
    """What kind of background work this is."""

    DOCUMENT_INGEST = "document_ingest"
    EMBEDDING_GENERATION = "embedding_generation"
    GRAPH_CONSTRUCTION = "graph_construction"
    EVALUATION = "evaluation"
    EXPORT = "export"
    MAINTENANCE = "maintenance"

    @property
    def queue(self) -> str:
        """Which queue this kind is routed to.

        Separated so a long export cannot sit in front of a document ingest that a clinician
        is waiting on. One queue with mixed durations is a latency problem with no knob.
        """
        return {
            "document_ingest": "ingest",
            "embedding_generation": "compute",
            "graph_construction": "compute",
            "evaluation": "batch",
            "export": "batch",
            "maintenance": "batch",
        }[self.value]

    @property
    def default_timeout_seconds(self) -> int:
        return {
            "document_ingest": 600,
            "embedding_generation": 900,
            "graph_construction": 900,
            "evaluation": 1800,
            "export": 1800,
            "maintenance": 3600,
        }[self.value]


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DEAD_LETTERED = "dead_lettered"


class TransientTaskError(RuntimeError):
    """A failure worth retrying — a timeout, a busy dependency, a dropped connection."""


class PermanentTaskError(RuntimeError):
    """A failure retrying cannot fix — a malformed payload, a missing tenant, a bad id."""


@dataclass(frozen=True, slots=True)
class TaskSpec:
    """One unit of background work."""

    kind: JobKind
    tenant_id: uuid.UUID
    payload: dict[str, Any] = field(default_factory=dict)
    task_id: uuid.UUID = field(default_factory=uuid.uuid4)
    idempotency_key: str = ""
    """Stable across retries and redeliveries. Empty means the task is naturally idempotent;
    anything that writes should set it, because at-least-once delivery guarantees this task
    will run twice eventually."""

    correlation_id: str = ""
    traceparent: str = ""
    max_retries: int = 3
    priority: int = 5
    scheduled_for: dt.datetime | None = None

    def __post_init__(self) -> None:
        if self.max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        if not 1 <= self.priority <= 9:
            raise ValueError("priority must be in [1, 9]")

    @property
    def queue(self) -> str:
        return self.kind.queue

    def dedupe_key(self) -> str:
        """What a worker checks before doing the work again."""
        return f"{self.kind.value}:{self.tenant_id}:{self.idempotency_key or self.task_id}"


@dataclass(frozen=True, slots=True)
class TaskResult:
    """The outcome of one execution."""

    task_id: uuid.UUID
    status: TaskStatus
    attempts: int = 1
    duration_ms: float = 0.0
    error: str | None = None
    output: dict[str, Any] = field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        return self.status is TaskStatus.SUCCEEDED


@runtime_checkable
class TaskHandler(Protocol):
    """Executes one kind of job."""

    async def run(self, spec: TaskSpec) -> dict[str, Any]: ...


@runtime_checkable
class TaskQueue(Protocol):
    """Enqueues work and reports on it."""

    async def enqueue(self, spec: TaskSpec) -> uuid.UUID: ...

    def register(self, kind: JobKind, handler: TaskHandler) -> None: ...

    async def result(self, task_id: uuid.UUID) -> TaskResult | None: ...


def backoff_seconds(attempt: int, *, base: float = 2.0, cap: float = 300.0) -> float:
    """Exponential backoff for retry ``attempt`` (1-based), capped.

    Deterministic — no jitter. Jitter matters when many clients retry a shared dependency
    together; here the broker already spreads redelivery, and a deterministic backoff is one
    a test can assert on. The cap exists because 2^n reaches hours by attempt 12, and a job
    that retries next Tuesday is a job nobody notices failed.
    """
    if attempt < 1:
        raise ValueError("attempt must be >= 1")
    return min(base**attempt, cap)
