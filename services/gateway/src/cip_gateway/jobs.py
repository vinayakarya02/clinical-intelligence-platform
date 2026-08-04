"""Background job handlers.

Six kinds, each a thin adapter over work that already exists in Phases 1-3. The adapters do
three things the underlying pipelines do not: classify failures for the retry policy, emit the
lifecycle events, and stay idempotent under redelivery.

Failure classification is the part worth attention. A pipeline raises what it raises; the
queue needs to know whether retrying could possibly help. Getting that wrong in either
direction is expensive — retrying a malformed payload three times delays the real error, and
not retrying a dropped connection loses work that would have succeeded.
"""

from __future__ import annotations

import time
from typing import Any

from cip_core.logging import get_logger
from cip_platform.events.base import Event, EventBus, EventType
from cip_platform.observability.ai_metrics import AIMetrics
from cip_platform.tasks.base import (
    JobKind,
    PermanentTaskError,
    TaskSpec,
    TransientTaskError,
)

__all__ = [
    "DocumentIngestJob",
    "EmbeddingJob",
    "EvaluationJob",
    "ExportJob",
    "GraphConstructionJob",
    "MaintenanceJob",
    "register_jobs",
]

_log = get_logger(__name__)


class _JobBase:
    """Shared event emission and metric recording."""

    kind: JobKind

    def __init__(self, *, bus: EventBus | None = None, metrics: AIMetrics | None = None) -> None:
        self._bus = bus
        self._metrics = metrics

    async def run(self, spec: TaskSpec) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            output = await self.execute(spec)
        except (TransientTaskError, PermanentTaskError):
            self._record(started, "failed")
            raise
        except Exception as exc:
            self._record(started, "failed")
            raise self.classify(exc) from exc
        self._record(started, "succeeded")
        return output

    async def execute(self, spec: TaskSpec) -> dict[str, Any]:  # pragma: no cover - abstract
        raise NotImplementedError

    def classify(self, exc: Exception) -> Exception:
        """Decide whether ``exc`` is worth retrying.

        Value and key errors are payload problems that no retry can fix. Everything else is
        assumed transient, because most unclassified failures are transport-level and the
        retry cap bounds the cost of being wrong — the same reasoning as Phase 2's embedding
        retry.
        """
        if isinstance(exc, ValueError | KeyError | TypeError):
            return PermanentTaskError(f"{type(exc).__name__}: {exc}")
        return TransientTaskError(f"{type(exc).__name__}: {exc}")

    def _record(self, started: float, status: str) -> None:
        if self._metrics is not None:
            self._metrics.record_task(
                job_kind=self.kind.value,
                status=status,
                duration_seconds=time.perf_counter() - started,
            )

    async def _emit(self, spec: TaskSpec, event_type: EventType, **payload: Any) -> None:
        if self._bus is None:
            return
        await self._bus.publish(
            Event(
                type=event_type,
                tenant_id=spec.tenant_id,
                payload=payload,
                correlation_id=spec.correlation_id,
                traceparent=spec.traceparent,
                source=f"worker/{self.kind.value}",
            )
        )


class DocumentIngestJob(_JobBase):
    """Runs the Phase 1 ETL for one document."""

    kind = JobKind.DOCUMENT_INGEST

    def __init__(self, pipeline: Any = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._pipeline = pipeline

    async def execute(self, spec: TaskSpec) -> dict[str, Any]:
        document_id = spec.payload.get("document_id")
        if not document_id:
            raise PermanentTaskError("document_ingest requires a document_id")

        if self._pipeline is None:
            # No pipeline wired is a deployment error, not a data error: retrying will not
            # produce one.
            raise PermanentTaskError("No ingestion pipeline is configured")

        result = await self._pipeline.process(spec.payload)
        await self._emit(spec, EventType.DOCUMENT_PARSED, document_id=document_id)
        chunk_count = int(result.get("chunks", 0)) if isinstance(result, dict) else 0
        if chunk_count:
            await self._emit(
                spec, EventType.CHUNK_CREATED, document_id=document_id, count=chunk_count
            )
        return {"document_id": document_id, "chunks": chunk_count}


class EmbeddingJob(_JobBase):
    """Generates embeddings for a batch of chunks."""

    kind = JobKind.EMBEDDING_GENERATION

    def __init__(self, service: Any = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._service = service

    async def execute(self, spec: TaskSpec) -> dict[str, Any]:
        texts = spec.payload.get("texts") or []
        if not texts:
            raise PermanentTaskError("embedding_generation requires texts")
        if self._service is None:
            raise PermanentTaskError("No embedding service is configured")

        batch = await self._service.embed_texts(list(texts))
        await self._emit(
            spec,
            EventType.EMBEDDING_GENERATED,
            count=len(batch.vectors),
            model_key=batch.model.key,
        )
        return {"count": len(batch.vectors), "model_key": batch.model.key}


class GraphConstructionJob(_JobBase):
    """Writes extracted entities and relationships to the graph."""

    kind = JobKind.GRAPH_CONSTRUCTION

    def __init__(self, store: Any = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._store = store

    async def execute(self, spec: TaskSpec) -> dict[str, Any]:
        nodes = spec.payload.get("nodes") or []
        edges = spec.payload.get("relationships") or []
        if self._store is None:
            raise PermanentTaskError("No graph store is configured")

        written_nodes = await self._store.upsert_nodes(list(nodes)) if nodes else 0
        written_edges = await self._store.upsert_relationships(list(edges)) if edges else 0
        await self._emit(
            spec, EventType.GRAPH_UPDATED, nodes=written_nodes, relationships=written_edges
        )
        return {"nodes": written_nodes, "relationships": written_edges}


class EvaluationJob(_JobBase):
    """Runs an evaluation set and records the result."""

    kind = JobKind.EVALUATION

    def __init__(self, evaluator: Any = None, tracker: Any = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._evaluator = evaluator
        self._tracker = tracker

    async def execute(self, spec: TaskSpec) -> dict[str, Any]:
        if self._evaluator is None:
            raise PermanentTaskError("No evaluator is configured")

        run_id = None
        if self._tracker is not None:
            run_id = self._tracker.start_run(
                spec.payload.get("experiment", "scheduled-evaluation"),
                params=spec.payload.get("params", {}),
            )
        report = await self._evaluator()
        metrics = dict(getattr(report, "metrics", {}) or {})
        if self._tracker is not None and run_id is not None:
            self._tracker.log_metrics(run_id, metrics)
            self._tracker.end_run(run_id)

        await self._emit(
            spec, EventType.EVALUATION_COMPLETED, run_id=run_id, metric_count=len(metrics)
        )
        return {"run_id": run_id, "metrics": metrics}


class ExportJob(_JobBase):
    """Exports a tenant's data.

    Requires an idempotency key: an export writes a file, and a redelivered export that writes
    a second one is a duplicate a human has to reconcile.
    """

    kind = JobKind.EXPORT

    def __init__(self, exporter: Any = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._exporter = exporter

    async def execute(self, spec: TaskSpec) -> dict[str, Any]:
        if not spec.idempotency_key:
            raise PermanentTaskError("export requires an idempotency_key")
        if self._exporter is None:
            raise PermanentTaskError("No exporter is configured")
        uri = await self._exporter(spec.tenant_id, spec.payload)
        return {"uri": uri}


class MaintenanceJob(_JobBase):
    """Scheduled housekeeping — cache sweeps, audit-chain verification, index checks."""

    kind = JobKind.MAINTENANCE

    def __init__(self, tasks: dict[str, Any] | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._tasks = tasks or {}

    async def execute(self, spec: TaskSpec) -> dict[str, Any]:
        name = spec.payload.get("task")
        handler = self._tasks.get(str(name))
        if handler is None:
            raise PermanentTaskError(f"Unknown maintenance task '{name}'")
        result = await handler(spec.tenant_id, spec.payload)
        return {"task": name, "result": result}


def register_jobs(queue: Any, jobs: list[_JobBase]) -> None:
    """Register every job with the queue."""
    for job in jobs:
        queue.register(job.kind, job)
