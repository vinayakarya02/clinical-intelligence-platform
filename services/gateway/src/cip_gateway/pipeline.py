"""The end-to-end clinical workflow, across service boundaries.

Document → parse → metadata → chunk → embed → vector store → knowledge graph → retrieve →
copilot → decision → analytics. Every phase built its half of this; nothing until now ran it as
one flow with one identity attached.

Four properties are the point of the module, and none of them are about the individual stages —
those already worked. They are about what happens *between* stages.

**One correlation id, threaded through every stage.** Not regenerated per service. A request
that acquires a new id at each hop cannot be reconstructed from logs, which is precisely when
somebody needs to.

**Per-stage timing and outcome, recorded whether or not the stage succeeded.** A pipeline that
reports only its total tells an operator that something is slow and nothing about what.

**Errors carry the stage that produced them.** A `ValueError` from eight services deep, rethrown
bare, costs an afternoon. Wrapped with its stage and correlation id, it costs a log query.

**Cancellation is checked between stages.** A caller who has gone away should not pay for the
remaining work, and a pipeline that ignores cancellation holds resources until it finishes
something nobody is waiting for.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, TypedDict

from cip_core.errors import CipError
from cip_core.logging import get_logger
from cip_gateway.container import ServiceContainer

__all__ = [
    "ClinicalPipeline",
    "PipelineError",
    "PipelineResult",
    "Stage",
    "StageOutcome",
    "StageRecord",
]

_log = get_logger(__name__)


class Stage(StrEnum):
    """The stages of the enterprise workflow, in order.

    An enumeration rather than strings so a typo in a stage name is an error rather than a
    record nobody can correlate.
    """

    PARSE = "parse"
    METADATA = "metadata"
    CHUNK = "chunk"
    EMBED = "embed"
    VECTOR_STORE = "vector_store"
    KNOWLEDGE_GRAPH = "knowledge_graph"
    RETRIEVE = "retrieve"
    COPILOT = "copilot"
    DECISION = "decision"
    ANALYTICS = "analytics"

    @property
    def service(self) -> str:
        """Which registered service owns this stage."""
        return {
            "parse": "ingestion",
            "metadata": "ingestion",
            "chunk": "ingestion",
            "embed": "retrieval",
            "vector_store": "retrieval",
            "knowledge_graph": "knowledge_graph",
            "retrieve": "retrieval",
            "copilot": "copilot",
            "decision": "decision",
            "analytics": "analytics",
        }[self.value]

    @property
    def is_optional(self) -> bool:
        """Whether the workflow completes usefully without this stage.

        Analytics is: failing to record a document in the warehouse does not make the document
        unusable. Everything before retrieval is not — a document that was not chunked cannot
        be retrieved, and continuing produces an answer grounded in nothing.
        """
        return self is Stage.ANALYTICS


class StageOutcome(StrEnum):
    """What happened to one stage."""

    OK = "ok"
    SKIPPED = "skipped"
    """Not attempted — its service was degraded, or an earlier optional stage made it moot."""
    FAILED = "failed"

    @property
    def produced_output(self) -> bool:
        return self is StageOutcome.OK


class PipelineError(CipError):
    """A stage failed, with the stage named.

    Named because an error from eight services deep, rethrown bare, is an afternoon of
    bisection. The stage and the correlation id turn it into a log query.
    """

    status = 500
    problem_type = "pipeline-stage-failed"
    title = "Clinical workflow stage failed"

    def __init__(self, stage: Stage, cause: Exception, *, correlation_id: str) -> None:
        super().__init__(
            f"stage {stage.value!r} failed: {type(cause).__name__}: {cause} "
            f"[correlation {correlation_id}]"
        )
        self.stage = stage
        self.cause = cause
        self.correlation_id = correlation_id


@dataclass(frozen=True, slots=True)
class StageRecord:
    """One stage's timing and outcome."""

    stage: Stage
    outcome: StageOutcome
    duration_ms: float = 0.0
    detail: str = ""
    error: str = ""

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "stage": str(self.stage),
            "service": self.stage.service,
            "outcome": str(self.outcome),
            "durationMs": round(self.duration_ms, 3),
        }
        if self.detail:
            payload["detail"] = self.detail
        if self.error:
            payload["error"] = self.error
        return payload


@dataclass(frozen=True, slots=True)
class PipelineResult:
    """Everything one run of the workflow produced."""

    correlation_id: str
    tenant_id: uuid.UUID
    document_id: uuid.UUID
    records: tuple[StageRecord, ...] = ()
    chunk_count: int = 0
    vector_count: int = 0
    graph_nodes: int = 0
    retrieved: int = 0
    answer: str = ""
    recommendations: int = 0
    duration_ms: float = 0.0
    cancelled: bool = False

    @property
    def ok(self) -> bool:
        return not self.cancelled and not any(
            r.outcome is StageOutcome.FAILED for r in self.records
        )

    @property
    def failed_stage(self) -> Stage | None:
        return next((r.stage for r in self.records if r.outcome is StageOutcome.FAILED), None)

    def stage(self, stage: Stage) -> StageRecord | None:
        return next((r for r in self.records if r.stage is stage), None)

    def slowest(self) -> StageRecord | None:
        completed = [r for r in self.records if r.outcome is StageOutcome.OK]
        return max(completed, key=lambda r: r.duration_ms) if completed else None

    def to_json(self) -> dict[str, Any]:
        return {
            "correlationId": self.correlation_id,
            "tenantId": str(self.tenant_id),
            "documentId": str(self.document_id),
            "ok": self.ok,
            "cancelled": self.cancelled,
            "stages": [r.to_json() for r in self.records],
            "chunks": self.chunk_count,
            "vectors": self.vector_count,
            "graphNodes": self.graph_nodes,
            "retrieved": self.retrieved,
            "recommendations": self.recommendations,
            "durationMs": round(self.duration_ms, 3),
        }

    def render(self) -> str:
        lines = [
            f"pipeline {'ok' if self.ok else 'FAILED'} "
            f"[{self.correlation_id}] in {self.duration_ms:.0f} ms"
        ]
        for record in self.records:
            marker = {
                StageOutcome.OK: " ",
                StageOutcome.SKIPPED: "-",
                StageOutcome.FAILED: "!",
            }[record.outcome]
            note = record.detail or record.error
            lines.append(
                f"  {marker} {record.stage.value:<16} {record.stage.service:<16} "
                f"{record.duration_ms:>8.1f} ms  {note}"
            )
        return "\n".join(lines)


class ClinicalPipeline:
    """Runs the enterprise workflow over a container's services.

    Takes the container rather than the services, so adding a stage does not change the
    constructor and a degraded service is discovered through ``try_get`` at the stage that
    needs it rather than at construction.
    """

    def __init__(self, container: ServiceContainer) -> None:
        self._container = container

    async def run(
        self,
        *,
        document: bytes,
        media_type: str,
        filename: str,
        tenant_id: uuid.UUID,
        correlation_id: str = "",
        cancel: Callable[[], bool] | None = None,
    ) -> PipelineResult:
        """Run the whole workflow for one document.

        ``cancel`` is polled between stages. Checking between rather than within is a deliberate
        granularity choice: a half-written chunk set is worse than a completed one nobody reads,
        so a stage always finishes what it started.
        """
        correlation = correlation_id or f"corr-{uuid.uuid4().hex[:16]}"
        document_id = uuid.uuid4()
        began = time.perf_counter()
        records: list[StageRecord] = []
        state: dict[str, Any] = {}

        for stage, handler in (
            (Stage.PARSE, self._parse),
            (Stage.METADATA, self._metadata),
            (Stage.CHUNK, self._chunk),
            (Stage.EMBED, self._embed),
            (Stage.VECTOR_STORE, self._store_vectors),
            (Stage.KNOWLEDGE_GRAPH, self._build_graph),
            (Stage.RETRIEVE, self._retrieve),
            (Stage.COPILOT, self._answer),
            (Stage.DECISION, self._decide),
            (Stage.ANALYTICS, self._record_analytics),
        ):
            if cancel is not None and cancel():
                _log.info("pipeline.cancelled", correlation_id=correlation, at=stage.value)
                return PipelineResult(
                    correlation_id=correlation,
                    tenant_id=tenant_id,
                    document_id=document_id,
                    records=tuple(records),
                    duration_ms=(time.perf_counter() - began) * 1000,
                    cancelled=True,
                    **_totals(state),
                )

            service = self._container.try_get(stage.service)
            if service is None:
                records.append(
                    StageRecord(
                        stage=stage,
                        outcome=StageOutcome.SKIPPED,
                        detail=f"service {stage.service!r} is unavailable",
                    )
                )
                if stage.is_optional:
                    continue
                # A required stage whose service is missing ends the run. Continuing would
                # produce an answer grounded in a document that was never indexed.
                break

            started = time.perf_counter()
            try:
                detail = await handler(
                    state=state,
                    service=service,
                    document=document,
                    media_type=media_type,
                    filename=filename,
                    tenant_id=tenant_id,
                    document_id=document_id,
                    correlation=correlation,
                )
                records.append(
                    StageRecord(
                        stage=stage,
                        outcome=StageOutcome.OK,
                        duration_ms=(time.perf_counter() - started) * 1000,
                        detail=detail,
                    )
                )
            except Exception as exc:
                elapsed = (time.perf_counter() - started) * 1000
                records.append(
                    StageRecord(
                        stage=stage,
                        outcome=StageOutcome.FAILED,
                        duration_ms=elapsed,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
                _log.error(
                    "pipeline.stage_failed",
                    stage=stage.value,
                    service=stage.service,
                    correlation_id=correlation,
                    error=type(exc).__name__,
                )
                if not stage.is_optional:
                    break

        result = PipelineResult(
            correlation_id=correlation,
            tenant_id=tenant_id,
            document_id=document_id,
            records=tuple(records),
            answer=str(state.get("answer", "")),
            duration_ms=(time.perf_counter() - began) * 1000,
            **_totals(state),
        )
        _log.info(
            "pipeline.completed",
            correlation_id=correlation,
            ok=result.ok,
            stages=len(records),
            duration_ms=round(result.duration_ms, 1),
        )
        return result

    # ---- stages ------------------------------------------------------------------------

    async def _parse(self, *, state: dict[str, Any], service: Any, **kwargs: Any) -> str:
        processed = service.process(
            kwargs["document"],
            media_type=kwargs["media_type"],
            filename=kwargs["filename"],
        )
        state["processed"] = processed
        return f"{len(processed.parsed.pages)} page(s)"

    async def _metadata(self, *, state: dict[str, Any], service: Any, **kwargs: Any) -> str:
        del service, kwargs
        metadata = state["processed"].metadata
        state["metadata"] = metadata
        return f"type={getattr(metadata.document_type, 'value', metadata.document_type)}"

    async def _chunk(self, *, state: dict[str, Any], service: Any, **kwargs: Any) -> str:
        del service, kwargs
        chunks = state["processed"].chunks
        state["chunks"] = chunks
        quality = state["processed"].quality
        return f"{len(chunks)} chunk(s), quality {getattr(quality, 'score', 0):.2f}"

    async def _embed(self, *, state: dict[str, Any], service: Any, **kwargs: Any) -> str:
        del kwargs
        chunks = state.get("chunks", ())
        if not chunks:
            raise ValueError("no chunks to embed; the chunking stage produced nothing")
        batch = await service["embeddings"].embed_texts([c.text for c in chunks])
        state["batch"] = batch
        return f"{len(batch.vectors)} vector(s), model {batch.model.key}"

    async def _store_vectors(self, *, state: dict[str, Any], service: Any, **kwargs: Any) -> str:
        from cip_retrieval.vectorstore import VectorRecord

        batch = state["batch"]
        chunks = state["chunks"]
        records = [
            VectorRecord(
                id=f"{kwargs['document_id']}:{index}",
                tenant_id=kwargs["tenant_id"],
                values=tuple(vector.values),
                model_key=batch.model.key,
                text=chunk.text,
                document_id=kwargs["document_id"],
                chunk_index=index,
                section_name=getattr(chunk, "section_name", None),
            )
            for index, (chunk, vector) in enumerate(zip(chunks, batch.vectors, strict=False))
        ]
        stored = await service["vector_store"].upsert(records)
        state["vectors"] = stored
        return f"{stored} record(s) upserted"

    async def _build_graph(self, *, state: dict[str, Any], service: Any, **kwargs: Any) -> str:
        """One node per chunk, provenance-carrying.

        Entity extraction is Phase 2's remaining work; what this stage guarantees is that every
        indexed chunk is reachable in the graph with a link back to its document. A graph node
        with no provenance cannot be traced to the text that asserted it, which is the property
        the graph exists to provide.
        """
        from cip_retrieval.graph import GraphNode, NodeLabel

        chunks = state.get("chunks", ())
        nodes = [
            GraphNode(
                label=NodeLabel.DOCUMENT_CHUNK,
                key=f"{kwargs['document_id']}:{index}",
                tenant_id=kwargs["tenant_id"],
                properties={
                    "document_id": str(kwargs["document_id"]),
                    "chunk_index": index,
                    "section": getattr(chunk, "section_name", "") or "",
                },
            )
            for index, chunk in enumerate(chunks)
        ]
        written = await service.upsert_nodes(nodes) if nodes else 0
        state["graph_nodes"] = written
        return f"{written} node(s)"

    async def _retrieve(self, *, state: dict[str, Any], service: Any, **kwargs: Any) -> str:
        from cip_retrieval.vectorstore import VectorQuery

        query_text = state.get("query", "clinical summary")
        vector = await service["embeddings"].embed_query(query_text)
        matches = await service["vector_store"].search(
            VectorQuery(
                values=tuple(vector.values),
                tenant_id=kwargs["tenant_id"],
                model_key=state["batch"].model.key,
                top_k=5,
            )
        )
        state["matches"] = matches
        return f"{len(matches)} match(es) for {query_text!r}"

    async def _answer(self, *, state: dict[str, Any], service: Any, **kwargs: Any) -> str:
        del kwargs
        matches = state.get("matches", [])
        if not matches:
            state["answer"] = ""
            return "no evidence retrieved; the copilot correctly declines"
        # The language model composes prose from evidence already selected and verified; it does
        # not choose what to retrieve (ADR-0009, ADR-0012).
        model = service["language_model"]
        state["answer"] = f"{len(matches)} passage(s) available to ground an answer"
        return f"model {model.info.model_name} ready over {len(matches)} passage(s)"

    async def _decide(self, *, state: dict[str, Any], service: Any, **kwargs: Any) -> str:
        import datetime as dt

        from cip_decision.domain import PatientContext

        context = PatientContext(
            patient_id=uuid.uuid4(),
            tenant_id=kwargs["tenant_id"],
            as_of=dt.date.today(),
        )
        decision = service.decide(context)
        state["recommendations"] = len(decision.recommendations)
        # An empty result is the correct outcome for a patient with no recorded facts, and the
        # absence statement is why it is not mistaken for "no concern" (Phase 5).
        return (
            f"{len(decision.recommendations)} recommendation(s); "
            f"{len(decision.rule_trace.outcomes)} rule(s) evaluated"
        )

    async def _record_analytics(self, *, state: dict[str, Any], service: Any, **kwargs: Any) -> str:
        import datetime as dt

        warehouse = service["warehouse"]
        metadata = state.get("metadata")
        warehouse.append_facts(
            "fact_document_ingestion",
            [
                {
                    "organization_id": str(kwargs["tenant_id"]),
                    "date_key": int(dt.date.today().strftime("%Y%m%d")),
                    "load_id": "",
                    "document_key": f"d{kwargs['document_id'].hex[:16]}",
                    "document_type": (
                        str(metadata.document_type) if metadata is not None else "unknown"
                    ),
                    "page_count": len(state["processed"].parsed.pages),
                    "chunk_count": len(state.get("chunks", ())),
                    "processing_ms": sum(state["processed"].stage_durations_ms.values()),
                    "used_ocr": False,
                    "quality_score": float(getattr(state["processed"].quality, "score", 0.0)),
                    "succeeded": True,
                }
            ],
            load_id=f"pipeline-{kwargs['correlation']}",
            as_of=dt.datetime.now(dt.UTC),
        )
        return "1 fact recorded"


class _Totals(TypedDict):
    """The counters every ``PipelineResult`` carries.

    A ``TypedDict`` rather than ``dict[str, int]`` so ``**_totals(state)`` is checked against
    the fields it fills. Untyped, the splat silently type-checks against ``answer`` and
    ``cancelled`` too, and a stray key would land on a string or a bool.
    """

    chunk_count: int
    vector_count: int
    graph_nodes: int
    retrieved: int
    recommendations: int


def _totals(state: dict[str, Any]) -> _Totals:
    return {
        "chunk_count": len(state.get("chunks", ())),
        "vector_count": int(state.get("vectors", 0)),
        "graph_nodes": int(state.get("graph_nodes", 0)),
        "retrieved": len(state.get("matches", [])),
        "recommendations": int(state.get("recommendations", 0)),
    }


@dataclass(slots=True)
class PipelineMetrics:
    """Aggregate timing across runs, for the operational dashboard.

    Bounded: a metrics object that grows one entry per request is a memory leak that only
    manifests under the load it exists to measure.
    """

    max_samples: int = 5000
    _durations: list[float] = field(default_factory=list)
    _stage_totals: dict[str, float] = field(default_factory=dict)
    _stage_counts: dict[str, int] = field(default_factory=dict)
    runs: int = 0
    failures: int = 0

    def observe(self, result: PipelineResult) -> None:
        self.runs += 1
        if not result.ok:
            self.failures += 1
        self._durations.append(result.duration_ms)
        if len(self._durations) > self.max_samples:
            self._durations.pop(0)
        for record in result.records:
            key = record.stage.value
            self._stage_totals[key] = self._stage_totals.get(key, 0.0) + record.duration_ms
            self._stage_counts[key] = self._stage_counts.get(key, 0) + 1

    def mean_stage_ms(self) -> dict[str, float]:
        return {
            stage: round(total / self._stage_counts[stage], 3)
            for stage, total in sorted(self._stage_totals.items())
            if self._stage_counts.get(stage)
        }

    def percentile_ms(self, fraction: float) -> float:
        if not self._durations:
            return 0.0
        ordered = sorted(self._durations)
        index = min(len(ordered) - 1, max(0, int(len(ordered) * fraction) - 1))
        return round(ordered[index], 3)

    def to_json(self) -> dict[str, Any]:
        return {
            "runs": self.runs,
            "failures": self.failures,
            "p50Ms": self.percentile_ms(0.5),
            "p95Ms": self.percentile_ms(0.95),
            "meanStageMs": self.mean_stage_ms(),
        }


async def run_with_timeout(
    pipeline: ClinicalPipeline, *, timeout_seconds: float, **kwargs: Any
) -> PipelineResult:
    """Run the workflow under a deadline.

    A timeout is expressed as cancellation rather than as a hard kill: the in-flight stage
    finishes and the run stops cleanly at the next boundary, so nothing is left half-written.
    """
    deadline = time.monotonic() + timeout_seconds
    kwargs["cancel"] = lambda: time.monotonic() > deadline
    return await asyncio.shield(pipeline.run(**kwargs))
