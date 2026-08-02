"""Processor and end-to-end pipeline tests.

The pipeline tests run the real orchestrator against a real database and real parsers,
substituting only genuinely-external processes (MongoDB, object storage). They are the
tests that would catch an ordering or transaction-boundary regression.
"""

from __future__ import annotations

import uuid

import pytest

from cip_core.config import Settings
from cip_core.db.postgres import PostgresManager
from cip_core.errors import DuplicateDocumentError, PipelineError
from cip_core.models import IngestionStatus, QualityVerdict
from cip_core.models.enums import DocumentType, PipelineStage
from cip_core.tenancy import TenantContext
from cip_ingestion.parsers import ParserError, build_parser_registry
from cip_ingestion.pipeline import IngestionPipeline, IngestionRequest
from cip_ingestion.processor import DocumentProcessor, StageTimer
from cip_ingestion.repositories import (
    AuditRepository,
    ChunkRepository,
    DocumentRepository,
    IngestionRunRepository,
)
from tests.fakes import FailingStorage, FakeMongoManager, InMemoryStorage
from tests.fixtures.documents import DISCHARGE_SUMMARY_TEXT, LAB_REPORT_TEXT, build_pdf


@pytest.fixture
def processor(settings: Settings) -> DocumentProcessor:
    return DocumentProcessor(
        parsers=build_parser_registry(settings.ingestion),
        settings=settings.ingestion,
    )


@pytest.fixture
def storage_double() -> InMemoryStorage:
    return InMemoryStorage()


@pytest.fixture
def pipeline(
    settings: Settings,
    postgres: PostgresManager,
    mongo: FakeMongoManager,
    storage_double: InMemoryStorage,
    processor: DocumentProcessor,
) -> IngestionPipeline:
    return IngestionPipeline(
        settings=settings,
        postgres=postgres,
        mongo=mongo,  # type: ignore[arg-type]
        storage=storage_double,
        processor=processor,
    )


def _request(**overrides: object) -> IngestionRequest:
    values: dict[str, object] = {
        "data": DISCHARGE_SUMMARY_TEXT.encode(),
        "source_system": "epic",
        "filename": "discharge.txt",
        "declared_media_type": "text/plain",
    }
    values.update(overrides)
    return IngestionRequest(**values)  # type: ignore[arg-type]


class TestStageTimer:
    def test_records_stage_durations(self) -> None:
        timer = StageTimer()
        with timer.time(PipelineStage.PARSE):
            pass
        assert "parse" in timer.durations
        assert timer.total_ms >= 0

    def test_accumulates_repeated_stages(self) -> None:
        timer = StageTimer()
        for _ in range(3):
            with timer.time(PipelineStage.PARSE):
                pass
        assert len(timer.durations) == 1

    def test_records_the_duration_of_a_failing_stage(self) -> None:
        """How long a stage ran before failing is usually the interesting number."""
        timer = StageTimer()
        with pytest.raises(RuntimeError), timer.time(PipelineStage.CHUNK):
            raise RuntimeError("boom")
        assert "chunk" in timer.durations


class TestDocumentProcessor:
    def test_processes_text_end_to_end(self, processor: DocumentProcessor) -> None:
        result = processor.process(
            DISCHARGE_SUMMARY_TEXT.encode(), media_type="text/plain", filename="note.txt"
        )
        assert result.parser_name == "text"
        assert result.metadata.document_type is DocumentType.DISCHARGE_SUMMARY
        assert len(result.chunks) > 1
        assert result.quality.verdict is QualityVerdict.PASS

    def test_processes_pdf_end_to_end(self, processor: DocumentProcessor) -> None:
        result = processor.process(build_pdf(DISCHARGE_SUMMARY_TEXT), media_type="application/pdf")
        assert result.parser_name == "pdf"
        assert result.parsed.page_count >= 1
        assert result.chunks

    def test_records_every_stage_duration(self, processor: DocumentProcessor) -> None:
        result = processor.process(DISCHARGE_SUMMARY_TEXT.encode(), media_type="text/plain")
        for stage in ("parse", "normalize", "detect_sections", "chunk", "quality_check"):
            assert stage in result.stage_durations_ms

    def test_declared_document_type_overrides_classification(
        self, processor: DocumentProcessor
    ) -> None:
        """A source system that knows the type must not be second-guessed."""
        result = processor.process(
            LAB_REPORT_TEXT.encode(),
            media_type="text/plain",
            declared_document_type=DocumentType.TRIAL_PROTOCOL,
        )
        assert result.metadata.document_type is DocumentType.TRIAL_PROTOCOL
        assert result.metadata.document_type_confidence == 1.0

    def test_structured_document_types_are_not_chunked(
        self, settings: Settings, processor: DocumentProcessor
    ) -> None:
        """FHIR/HL7v2 populate the relational store directly; they are never embedded."""
        result = processor.process(
            b'{"resourceType": "Bundle", "entry": []}',
            media_type="text/plain",
            declared_document_type=DocumentType.FHIR_BUNDLE,
        )
        assert result.chunks == ()

    def test_unparseable_input_raises(self, processor: DocumentProcessor) -> None:
        with pytest.raises(ParserError):
            processor.process(b"", media_type="text/plain")

    def test_chunks_reference_valid_offsets(self, processor: DocumentProcessor) -> None:
        result = processor.process(DISCHARGE_SUMMARY_TEXT.encode(), media_type="text/plain")
        for chunk in result.chunks:
            assert result.normalized.text[chunk.char_start : chunk.char_end] == chunk.text


class TestIngestionPipeline:
    async def test_ingests_a_document_end_to_end(
        self,
        pipeline: IngestionPipeline,
        postgres: PostgresManager,
        context: TenantContext,
        storage_double: InMemoryStorage,
        mongo: FakeMongoManager,
    ) -> None:
        result = await pipeline.ingest(_request(), context=context)

        assert result.status is IngestionStatus.CHUNKED
        assert result.chunk_count > 0
        assert result.quality is not None
        assert result.run_id is not None

        assert storage_double.put_calls == 1
        assert len(mongo.documents) == 1

        async with postgres.tenant_session(context) as session:
            document = await DocumentRepository(session).get(result.document_id, context=context)
            assert document is not None
            assert document.ingestion_status == IngestionStatus.CHUNKED.value
            assert document.document_type == DocumentType.DISCHARGE_SUMMARY.value
            assert document.title == "DISCHARGE SUMMARY"
            assert document.language == "en"
            assert document.doc_metadata["section_names"]

            chunks = await ChunkRepository(session).list_for_document(
                result.document_id, context=context
            )
            assert len(chunks) == result.chunk_count

    async def test_raw_bytes_are_stored_under_a_content_addressed_key(
        self,
        pipeline: IngestionPipeline,
        context: TenantContext,
        storage_double: InMemoryStorage,
    ) -> None:
        result = await pipeline.ingest(_request(), context=context)
        key = next(iter(storage_double.objects))
        assert str(context.tenant_id) in key
        assert result.content_hash in key

    async def test_writes_an_audit_entry(
        self, pipeline: IngestionPipeline, postgres: PostgresManager, context: TenantContext
    ) -> None:
        result = await pipeline.ingest(_request(), context=context)

        async with postgres.tenant_session(context) as session:
            entries = await AuditRepository(session).load_chain(context.tenant_id)

        assert len(entries) == 1
        assert entries[0].action == "document.ingested"
        assert entries[0].resource_id == str(result.document_id)
        assert entries[0].phi_accessed is True

    async def test_records_an_ingestion_run(
        self, pipeline: IngestionPipeline, postgres: PostgresManager, context: TenantContext
    ) -> None:
        result = await pipeline.ingest(_request(), context=context)

        async with postgres.tenant_session(context) as session:
            runs = await IngestionRunRepository(session).list_for_document(
                result.document_id, context=context
            )
        assert runs[0].parser_name == "text"
        assert runs[0].pipeline_version
        assert runs[0].chunk_count == result.chunk_count

    async def test_duplicate_content_is_refused(
        self, pipeline: IngestionPipeline, context: TenantContext
    ) -> None:
        first = await pipeline.ingest(_request(), context=context)

        with pytest.raises(DuplicateDocumentError) as exc_info:
            await pipeline.ingest(_request(), context=context)
        assert exc_info.value.existing_document_id == str(first.document_id)

    async def test_force_reingest_reprocesses_in_place(
        self, pipeline: IngestionPipeline, postgres: PostgresManager, context: TenantContext
    ) -> None:
        first = await pipeline.ingest(_request(), context=context)
        second = await pipeline.ingest(_request(force_reingest=True), context=context)

        assert second.document_id == first.document_id

        async with postgres.tenant_session(context) as session:
            runs = await IngestionRunRepository(session).list_for_document(
                first.document_id, context=context
            )
        assert len(runs) == 2

    async def test_identical_content_from_another_source_is_a_separate_document(
        self, pipeline: IngestionPipeline, context: TenantContext
    ) -> None:
        first = await pipeline.ingest(_request(source_system="epic"), context=context)
        second = await pipeline.ingest(_request(source_system="cerner"), context=context)
        assert first.document_id != second.document_id

    async def test_identical_content_in_another_tenant_is_a_separate_document(
        self,
        pipeline: IngestionPipeline,
        context: TenantContext,
        other_context: TenantContext,
    ) -> None:
        first = await pipeline.ingest(_request(), context=context)
        second = await pipeline.ingest(_request(), context=other_context)
        assert first.document_id != second.document_id

    async def test_poor_quality_document_is_quarantined_not_failed(
        self, pipeline: IngestionPipeline, postgres: PostgresManager, context: TenantContext
    ) -> None:
        """Quarantine keeps the bytes and the audit trail; failure would not."""
        garbled = (chr(0xFFFD) * 500).encode("utf-8")
        result = await pipeline.ingest(
            _request(data=garbled, filename="garbled.txt"), context=context
        )

        assert result.status is IngestionStatus.QUARANTINED
        assert result.quality is not None
        assert result.quality.verdict is QualityVerdict.FAIL

        async with postgres.tenant_session(context) as session:
            document = await DocumentRepository(session).get(result.document_id, context=context)
        assert document is not None
        assert document.failure_reason

    async def test_quarantined_documents_are_not_queued_for_indexing(
        self, pipeline: IngestionPipeline, postgres: PostgresManager, context: TenantContext
    ) -> None:
        from sqlalchemy import select

        from cip_core.models.tables import IndexSyncState

        garbled = (chr(0xFFFD) * 500).encode("utf-8")
        result = await pipeline.ingest(_request(data=garbled), context=context)

        async with postgres.tenant_session(context) as session:
            rows = (
                (
                    await session.execute(
                        select(IndexSyncState).where(
                            IndexSyncState.document_id == result.document_id
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert rows == [], "a document the quality gate rejected must not be indexed"

    async def test_healthy_documents_are_queued_for_indexing(
        self, pipeline: IngestionPipeline, postgres: PostgresManager, context: TenantContext
    ) -> None:
        from sqlalchemy import select

        from cip_core.models.tables import IndexSyncState

        result = await pipeline.ingest(_request(), context=context)

        async with postgres.tenant_session(context) as session:
            rows = (
                (
                    await session.execute(
                        select(IndexSyncState).where(
                            IndexSyncState.document_id == result.document_id
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert {row.target_index for row in rows} == {"vector", "opensearch", "neo4j"}

    async def test_unrecognisable_payload_is_rejected_before_storage(
        self,
        pipeline: IngestionPipeline,
        context: TenantContext,
        storage_double: InMemoryStorage,
    ) -> None:
        """Validation runs first, so unusable bytes never reach storage or the database."""
        from cip_core.errors import UnsupportedMediaTypeError

        with pytest.raises(UnsupportedMediaTypeError):
            await pipeline.ingest(
                _request(
                    data=bytes([0x89, 0x50, 0x4E, 0x47, 0x00, 0x01]),
                    declared_media_type="application/octet-stream",
                ),
                context=context,
            )
        assert storage_double.put_calls == 0

    async def test_parse_failure_marks_the_document_failed_but_keeps_the_bytes(
        self,
        pipeline: IngestionPipeline,
        postgres: PostgresManager,
        context: TenantContext,
        storage_double: InMemoryStorage,
    ) -> None:
        """A truncated upload: valid PDF signature, unparseable content.

        The raw bytes must already be stored and the document row marked ``FAILED``, so
        the document is reprocessable once the cause is understood — losing the only copy
        at exactly the moment it becomes interesting is the failure mode this ordering
        exists to prevent.
        """
        truncated_pdf = build_pdf(DISCHARGE_SUMMARY_TEXT)[:200]

        with pytest.raises((PipelineError, ParserError)):
            await pipeline.ingest(
                _request(data=truncated_pdf, declared_media_type="application/pdf"),
                context=context,
            )

        assert storage_double.put_calls == 1, "raw bytes must be retained for reprocessing"

        async with postgres.tenant_session(context) as session:
            documents = await DocumentRepository(session).list_for_tenant(context=context)
        assert len(documents) == 1
        assert documents[0].ingestion_status == IngestionStatus.FAILED.value
        assert documents[0].failure_reason

        async with postgres.tenant_session(context) as session:
            runs = await IngestionRunRepository(session).list_for_document(
                documents[0].document_id, context=context
            )
        assert runs[0].failed_stage == "parse"

    async def test_storage_failure_prevents_a_document_row(
        self,
        settings: Settings,
        postgres: PostgresManager,
        mongo: FakeMongoManager,
        processor: DocumentProcessor,
        context: TenantContext,
    ) -> None:
        """Raw bytes are stored first, so a storage outage leaves no orphan row."""
        pipeline = IngestionPipeline(
            settings=settings,
            postgres=postgres,
            mongo=mongo,  # type: ignore[arg-type]
            storage=FailingStorage(),
            processor=processor,
        )
        with pytest.raises(OSError, match="simulated storage failure"):
            await pipeline.ingest(_request(), context=context)

        async with postgres.tenant_session(context) as session:
            documents = await DocumentRepository(session).list_for_tenant(context=context)
        assert documents == []

    async def test_write_scope_is_required(
        self, pipeline: IngestionPipeline, tenant_id: uuid.UUID
    ) -> None:
        from cip_core.errors import AuthorizationError

        read_only = TenantContext(
            tenant_id=tenant_id, actor_id="viewer", scopes=frozenset({"documents:read"})
        )
        with pytest.raises(AuthorizationError, match="documents:write"):
            await pipeline.ingest(_request(), context=read_only)

    async def test_parsed_artifact_is_persisted(
        self, pipeline: IngestionPipeline, context: TenantContext, mongo: FakeMongoManager
    ) -> None:
        result = await pipeline.ingest(_request(), context=context)
        artifact = mongo.documents[(str(context.tenant_id), str(result.document_id))]

        assert artifact["parser_name"] == "text"
        assert artifact["page_count"] >= 1
        assert artifact["pages"][0]["blocks"]
        assert artifact["content_hash"] == result.content_hash

    async def test_deidentified_documents_are_not_flagged_as_phi_access(
        self, pipeline: IngestionPipeline, postgres: PostgresManager, context: TenantContext
    ) -> None:
        from cip_core.models.enums import DeidentificationStatus

        await pipeline.ingest(
            _request(deidentification_status=DeidentificationStatus.SAFE_HARBOR),
            context=context,
        )
        async with postgres.tenant_session(context) as session:
            entries = await AuditRepository(session).load_chain(context.tenant_id)
        assert entries[0].phi_accessed is False
