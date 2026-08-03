"""ETL orchestration.

Wraps the pure :class:`~cip_ingestion.processor.DocumentProcessor` with the I/O the
pipeline needs: validation, duplicate detection, raw-object storage, and persistence of
the document, its chunks, its parsed artifact, its quality report, and its audit trail.

Ordering here is not arbitrary. Raw bytes are stored **before** parsing, so a document
that crashes a parser is still durably retained and reprocessable once the parser is
fixed — the alternative loses the only copy at exactly the moment it becomes interesting.
Everything after the raw write happens in one database transaction, so a failure leaves
the document row marked ``FAILED`` with no half-written chunks rather than a document
that looks ingested but has partial content in retrieval.

Quality gating and pipeline failure are different outcomes: a ``FAIL`` verdict quarantines
a document (stored, auditable, withheld from retrieval, reprocessable) while an exception
marks it failed. Conflating them would either discard recoverable documents or admit
unreadable ones.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field

from sqlalchemy.exc import IntegrityError

from cip_core.audit import AuditRecord
from cip_core.config import Settings
from cip_core.db.mongo import MongoManager
from cip_core.db.postgres import PostgresManager
from cip_core.errors import CipError, DuplicateDocumentError, PipelineError
from cip_core.logging import get_logger
from cip_core.models import (
    DeidentificationStatus,
    Document,
    DocumentType,
    IngestionStatus,
    PipelineStage,
    QualityVerdict,
    SyncTarget,
)
from cip_core.storage import ObjectStorage, build_object_key
from cip_core.tenancy import TenantContext
from cip_ingestion.processing.quality import QualityReport
from cip_ingestion.processor import DocumentProcessor, ProcessingResult, StageTimer
from cip_ingestion.repositories import (
    AuditRepository,
    ChunkRepository,
    DocumentRepository,
    IngestionRunRepository,
    ParsedArtifactRepository,
    QualityReportRepository,
    SyncStateRepository,
)
from cip_ingestion.validation import validate_upload
from cip_ingestion.version import PIPELINE_VERSION

__all__ = ["IngestionPipeline", "IngestionRequest", "IngestionResult"]

_log = get_logger(__name__)

#: Downstream indexes a newly-ingested document becomes work for. Nothing consumes these
#: in Phase 1; see SyncStateRepository for why they are written now.
_SYNC_TARGETS = (SyncTarget.VECTOR, SyncTarget.OPENSEARCH, SyncTarget.NEO4J)


@dataclass(frozen=True, slots=True)
class IngestionRequest:
    """An ingestion request."""

    data: bytes
    source_system: str
    filename: str | None = None
    declared_media_type: str = "application/octet-stream"
    document_type: DocumentType | None = None
    patient_id: uuid.UUID | None = None
    deidentification_status: DeidentificationStatus = DeidentificationStatus.NOT_DEIDENTIFIED
    access_scope: dict = field(default_factory=dict)
    force_reingest: bool = False
    """Reprocess even if content-identical to an existing document. Used when the
    pipeline itself has changed, not to work around a duplicate."""


@dataclass(frozen=True, slots=True)
class IngestionResult:
    """Outcome of an ingestion."""

    document_id: uuid.UUID
    status: IngestionStatus
    chunk_count: int
    content_hash: str
    run_id: uuid.UUID | None = None
    quality: QualityReport | None = None
    stage_durations_ms: dict[str, float] = field(default_factory=dict)


class IngestionPipeline:
    """Orchestrates the end-to-end document ingestion flow."""

    def __init__(
        self,
        *,
        settings: Settings,
        postgres: PostgresManager,
        mongo: MongoManager,
        storage: ObjectStorage,
        processor: DocumentProcessor,
    ) -> None:
        self._settings = settings
        self._postgres = postgres
        self._mongo = mongo
        self._storage = storage
        self._processor = processor

    async def ingest(self, request: IngestionRequest, *, context: TenantContext) -> IngestionResult:
        """Run the full pipeline for one document."""
        context.require_scope("documents:write")
        timer = StageTimer()

        with timer.time(PipelineStage.VALIDATE):
            upload = validate_upload(
                request.data,
                declared_media_type=request.declared_media_type,
                filename=request.filename,
                settings=self._settings.ingestion,
            )

        existing = await self._find_duplicate(
            upload.content_hash, source_system=request.source_system, context=context
        )
        if existing is not None and not request.force_reingest:
            _log.info(
                "ingest.duplicate",
                document_id=str(existing.document_id),
                content_hash=upload.content_hash,
            )
            raise DuplicateDocumentError(
                "A document with identical content has already been ingested from this "
                "source system",
                existing_document_id=str(existing.document_id),
                content_hash=upload.content_hash,
            )

        object_key = build_object_key(
            tenant_id=context.tenant_id,
            content_hash=upload.content_hash,
            extension=upload.extension,
        )
        stored = await self._storage.put(object_key, upload.data, content_type=upload.media_type)

        document_id = existing.document_id if existing is not None else uuid.uuid4()
        run_id: uuid.UUID | None = None

        try:
            async with self._postgres.tenant_session(context) as session:
                documents = DocumentRepository(session)
                runs = IngestionRunRepository(session)

                if existing is None:
                    document = Document(
                        document_id=document_id,
                        tenant_id=context.tenant_id,
                        patient_id=request.patient_id,
                        document_type=str(request.document_type or DocumentType.UNKNOWN),
                        source_system=request.source_system,
                        original_filename=upload.filename,
                        media_type=upload.media_type,
                        size_bytes=upload.size_bytes,
                        content_hash=upload.content_hash,
                        object_storage_uri=stored.uri,
                        object_storage_key=stored.key,
                        deidentification_status=str(request.deidentification_status),
                        access_scope=request.access_scope,
                        ingestion_status=IngestionStatus.PENDING.value,
                    )
                    await documents.add(document, context=context)

                run = await runs.start(
                    document_id=document_id,
                    context=context,
                    pipeline_version=PIPELINE_VERSION,
                )
                run_id = run.run_id
        except IntegrityError as exc:
            # The pre-check above runs in its own transaction, so two concurrent uploads of
            # identical content both see "no duplicate" and both insert. The unique
            # constraint on (tenant_id, source_system, content_hash) is what actually
            # arbitrates; without translating it here the loser gets a 500 for what is
            # really a 409, and the API contract in docs/api/openapi.yaml would be a lie
            # under exactly the concurrency a bulk load produces.
            loser = await self._find_duplicate(
                upload.content_hash, source_system=request.source_system, context=context
            )
            _log.info(
                "ingest.duplicate_race",
                content_hash=upload.content_hash,
                winner_document_id=str(loser.document_id) if loser else None,
            )
            raise DuplicateDocumentError(
                "A document with identical content has already been ingested from this "
                "source system",
                existing_document_id=str(loser.document_id) if loser else str(document_id),
                content_hash=upload.content_hash,
            ) from exc
        except CipError:
            raise
        except Exception as exc:
            raise PipelineError(
                f"Failed to record document: {type(exc).__name__}",
                stage=str(PipelineStage.PERSIST_RAW),
            ) from exc

        try:
            result = self._processor.process(
                upload.data,
                media_type=upload.media_type,
                filename=upload.filename,
                declared_document_type=request.document_type,
                timer=timer,
            )
        except Exception as exc:
            await self._record_failure(
                document_id=document_id,
                run_id=run_id,
                context=context,
                stage=PipelineStage.PARSE,
                reason=f"{type(exc).__name__}: {exc}",
                timer=timer,
            )
            if isinstance(exc, CipError):
                raise
            raise PipelineError(
                f"Processing failed: {type(exc).__name__}", stage=str(PipelineStage.PARSE)
            ) from exc

        status = (
            IngestionStatus.QUARANTINED
            if result.quality.verdict is QualityVerdict.FAIL
            else IngestionStatus.CHUNKED
        )

        try:
            await self._persist_results(
                document_id=document_id,
                run_id=run_id,
                context=context,
                request=request,
                result=result,
                status=status,
                content_hash=upload.content_hash,
                timer=timer,
            )
        except Exception as exc:
            await self._record_failure(
                document_id=document_id,
                run_id=run_id,
                context=context,
                stage=PipelineStage.PERSIST_ARTIFACTS,
                reason=f"{type(exc).__name__}: {exc}",
                timer=timer,
            )
            raise PipelineError(
                f"Persisting results failed: {type(exc).__name__}",
                stage=str(PipelineStage.PERSIST_ARTIFACTS),
            ) from exc

        _log.info(
            "ingest.completed",
            document_id=str(document_id),
            status=str(status),
            chunk_count=len(result.chunks),
            quality_verdict=str(result.quality.verdict),
            duration_ms=timer.total_ms,
        )

        return IngestionResult(
            document_id=document_id,
            status=status,
            chunk_count=len(result.chunks),
            content_hash=upload.content_hash,
            run_id=run_id,
            quality=result.quality,
            stage_durations_ms=timer.durations,
        )

    async def _find_duplicate(
        self, content_hash: str, *, source_system: str, context: TenantContext
    ) -> Document | None:
        async with self._postgres.tenant_session(context) as session:
            return await DocumentRepository(session).find_by_content_hash(
                content_hash, source_system=source_system, context=context
            )

    async def _persist_results(
        self,
        *,
        document_id: uuid.UUID,
        run_id: uuid.UUID | None,
        context: TenantContext,
        request: IngestionRequest,
        result: ProcessingResult,
        status: IngestionStatus,
        content_hash: str,
        timer: StageTimer,
    ) -> None:
        """Persist all derived artifacts in one transaction."""
        # The Mongo artifact is written first and deliberately outside the SQL
        # transaction: the two stores cannot participate in one transaction, and an
        # orphaned artifact (written, then SQL rolls back) is harmless because it is
        # keyed by document_id and overwritten on the next run. The reverse ordering
        # would leave a document row referencing an artifact that does not exist.
        artifacts = ParsedArtifactRepository(
            self._mongo, collection_name=self._settings.mongo.parsed_documents_collection
        )
        await artifacts.save(
            result.parsed,
            document_id=document_id,
            content_hash=content_hash,
            context=context,
        )

        async with self._postgres.tenant_session(context) as session:
            documents = DocumentRepository(session)
            chunks = ChunkRepository(session)
            runs = IngestionRunRepository(session)
            quality = QualityReportRepository(session)
            sync = SyncStateRepository(session)
            audit = AuditRepository(session)

            chunk_count = await chunks.replace_for_document(
                document_id,
                result.chunks,
                context=context,
                patient_id=request.patient_id,
            )

            await documents.apply_extraction_results(
                document_id,
                context=context,
                title=result.metadata.title,
                page_count=result.parsed.page_count,
                language=result.metadata.language,
                effective_date=result.metadata.effective_date,
                metadata=result.metadata.to_json(),
                document_type=str(result.metadata.document_type),
            )
            await documents.set_status(
                document_id,
                status,
                context=context,
                failure_reason=(
                    "; ".join(check.detail for check in result.quality.failed_checks)
                    if status is IngestionStatus.QUARANTINED
                    else None
                ),
            )

            await quality.add(
                result.quality, document_id=document_id, run_id=run_id, context=context
            )

            # Quarantined documents are not queued for downstream indexing: admitting a
            # document the quality gate rejected is the outcome the gate exists to prevent.
            if status is not IngestionStatus.QUARANTINED:
                await sync.mark_pending(document_id, _SYNC_TARGETS)

            if run_id is not None:
                await runs.complete(
                    run_id,
                    context=context,
                    status=status,
                    stage_durations_ms=timer.durations,
                    chunk_count=chunk_count,
                    parser_name=result.parser_name,
                )

            await audit.append(
                AuditRecord(
                    tenant_id=context.tenant_id,
                    action="document.ingested",
                    resource_type="document",
                    resource_id=str(document_id),
                    actor_user_id=context.actor_id,
                    actor_service=self._settings.service_name,
                    # Every clinical document is assumed to contain PHI until a
                    # de-identification pass says otherwise. Assuming the opposite would
                    # under-report PHI access in exactly the audit trail regulators read.
                    phi_accessed=(
                        request.deidentification_status is DeidentificationStatus.NOT_DEIDENTIFIED
                    ),
                    request_context={
                        "source_system": request.source_system,
                        "content_hash": content_hash,
                        "chunk_count": chunk_count,
                        "quality_verdict": str(result.quality.verdict),
                        "quality_score": result.quality.score,
                        "pipeline_version": PIPELINE_VERSION,
                        "parser": result.parser_name,
                        "request_id": context.request_id,
                    },
                    occurred_at=dt.datetime.now(dt.UTC),
                )
            )

    async def _record_failure(
        self,
        *,
        document_id: uuid.UUID,
        run_id: uuid.UUID | None,
        context: TenantContext,
        stage: PipelineStage,
        reason: str,
        timer: StageTimer,
    ) -> None:
        """Mark a document and its run as failed.

        Failures here are logged and swallowed: this runs on an error path, and raising a
        secondary exception would replace the original diagnosis with a less useful one.
        """
        try:
            async with self._postgres.tenant_session(context) as session:
                await DocumentRepository(session).set_status(
                    document_id,
                    IngestionStatus.FAILED,
                    context=context,
                    failure_reason=reason[:1000],
                )
                if run_id is not None:
                    await IngestionRunRepository(session).complete(
                        run_id,
                        context=context,
                        status=IngestionStatus.FAILED,
                        stage_durations_ms=timer.durations,
                        chunk_count=0,
                        failed_stage=str(stage),
                        failure_reason=reason[:1000],
                    )
        except Exception:
            _log.exception("ingest.failure_record_failed", document_id=str(document_id))

        _log.error("ingest.failed", document_id=str(document_id), stage=str(stage), reason=reason)
