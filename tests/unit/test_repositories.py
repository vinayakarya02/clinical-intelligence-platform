"""Persistence tests, run against a real (in-memory) database.

Tenant isolation is asserted at the repository level here. Row-Level Security is the
database-enforced floor in production, but SQLite has no RLS — so these tests prove the
*application-level* half of the defence in depth, which is the half that must hold even
when a connection's session variable was never set.
"""

from __future__ import annotations

import datetime as dt
import uuid
from itertools import pairwise

import pytest
from sqlalchemy import select

from cip_core.audit import AuditRecord, verify_chain
from cip_core.db.postgres import PostgresManager
from cip_core.models import Document, DocumentChunk, IngestionStatus, SyncTarget
from cip_core.models.enums import QualityVerdict
from cip_core.models.tables import IndexSyncState
from cip_core.tenancy import TenantContext
from cip_ingestion.domain import TextChunk
from cip_ingestion.processing.quality import QualityCheck, QualityReport
from cip_ingestion.repositories import (
    AuditRepository,
    ChunkRepository,
    DocumentRepository,
    IngestionRunRepository,
    QualityReportRepository,
    SyncStateRepository,
)


def _document(
    tenant_id: uuid.UUID, *, content_hash: str = "a" * 64, **overrides: object
) -> Document:
    values: dict[str, object] = {
        "tenant_id": tenant_id,
        "document_type": "discharge_summary",
        "source_system": "epic",
        "media_type": "text/plain",
        "size_bytes": 1024,
        "content_hash": content_hash,
        "object_storage_uri": "memory://doc",
        "object_storage_key": f"tenants/{tenant_id}/documents/2026/03/{content_hash}.txt",
        "ingestion_status": IngestionStatus.PENDING.value,
    }
    values.update(overrides)
    return Document(**values)  # type: ignore[arg-type]


def _chunks(count: int = 3) -> list[TextChunk]:
    return [
        TextChunk(
            index=i,
            text=f"Chunk number {i} of clinical narrative text.",
            char_start=i * 50,
            char_end=i * 50 + 44,
            token_count=12,
        )
        for i in range(count)
    ]


class TestDocumentRepository:
    async def test_add_and_get(self, postgres: PostgresManager, context: TenantContext) -> None:
        async with postgres.tenant_session(context) as session:
            document = await DocumentRepository(session).add(
                _document(context.tenant_id), context=context
            )
            document_id = document.document_id

        async with postgres.tenant_session(context) as session:
            fetched = await DocumentRepository(session).get(document_id, context=context)
            assert fetched is not None
            assert fetched.content_hash == "a" * 64

    async def test_add_rejects_a_foreign_tenant_document(
        self, postgres: PostgresManager, context: TenantContext, other_tenant_id: uuid.UUID
    ) -> None:
        from cip_core.errors import AuthorizationError

        async with postgres.tenant_session(context) as session:
            with pytest.raises(AuthorizationError):
                await DocumentRepository(session).add(_document(other_tenant_id), context=context)

    async def test_get_does_not_cross_tenants(
        self,
        postgres: PostgresManager,
        context: TenantContext,
        other_context: TenantContext,
    ) -> None:
        async with postgres.tenant_session(context) as session:
            document = await DocumentRepository(session).add(
                _document(context.tenant_id), context=context
            )
            document_id = document.document_id

        async with postgres.tenant_session(other_context) as session:
            assert (
                await DocumentRepository(session).get(document_id, context=other_context)
            ) is None

    async def test_find_by_content_hash(
        self, postgres: PostgresManager, context: TenantContext
    ) -> None:
        async with postgres.tenant_session(context) as session:
            await DocumentRepository(session).add(_document(context.tenant_id), context=context)

        async with postgres.tenant_session(context) as session:
            found = await DocumentRepository(session).find_by_content_hash(
                "a" * 64, source_system="epic", context=context
            )
            assert found is not None

    async def test_find_by_content_hash_is_scoped_by_source_system(
        self, postgres: PostgresManager, context: TenantContext
    ) -> None:
        """Identical bytes from two systems are two facts with different provenance."""
        async with postgres.tenant_session(context) as session:
            await DocumentRepository(session).add(_document(context.tenant_id), context=context)

        async with postgres.tenant_session(context) as session:
            assert (
                await DocumentRepository(session).find_by_content_hash(
                    "a" * 64, source_system="cerner", context=context
                )
            ) is None

    async def test_find_by_content_hash_does_not_cross_tenants(
        self,
        postgres: PostgresManager,
        context: TenantContext,
        other_context: TenantContext,
    ) -> None:
        async with postgres.tenant_session(context) as session:
            await DocumentRepository(session).add(_document(context.tenant_id), context=context)

        async with postgres.tenant_session(other_context) as session:
            assert (
                await DocumentRepository(session).find_by_content_hash(
                    "a" * 64, source_system="epic", context=other_context
                )
            ) is None

    async def test_set_status_clears_a_stale_failure_reason(
        self, postgres: PostgresManager, context: TenantContext
    ) -> None:
        """A recovered document must not keep a misleading failure message."""
        async with postgres.tenant_session(context) as session:
            document = await DocumentRepository(session).add(
                _document(context.tenant_id), context=context
            )
            document_id = document.document_id

        async with postgres.tenant_session(context) as session:
            await DocumentRepository(session).set_status(
                document_id,
                IngestionStatus.FAILED,
                context=context,
                failure_reason="parser blew up",
            )
        async with postgres.tenant_session(context) as session:
            await DocumentRepository(session).set_status(
                document_id, IngestionStatus.CHUNKED, context=context
            )
        async with postgres.tenant_session(context) as session:
            refreshed = await DocumentRepository(session).get(document_id, context=context)
            assert refreshed is not None
            assert refreshed.ingestion_status == IngestionStatus.CHUNKED.value
            assert refreshed.failure_reason is None

    async def test_list_filters_by_status(
        self, postgres: PostgresManager, context: TenantContext
    ) -> None:
        async with postgres.tenant_session(context) as session:
            repo = DocumentRepository(session)
            await repo.add(_document(context.tenant_id, content_hash="a" * 64), context=context)
            await repo.add(
                _document(
                    context.tenant_id,
                    content_hash="b" * 64,
                    ingestion_status=IngestionStatus.CHUNKED.value,
                ),
                context=context,
            )

        async with postgres.tenant_session(context) as session:
            chunked = await DocumentRepository(session).list_for_tenant(
                context=context, status=IngestionStatus.CHUNKED
            )
            assert len(chunked) == 1

    async def test_soft_delete_hides_the_document(
        self, postgres: PostgresManager, context: TenantContext
    ) -> None:
        async with postgres.tenant_session(context) as session:
            document = await DocumentRepository(session).add(
                _document(context.tenant_id), context=context
            )
            document_id = document.document_id

        async with postgres.tenant_session(context) as session:
            assert await DocumentRepository(session).soft_delete(
                document_id, context=context, purge_after=dt.date(2032, 1, 1)
            )

        async with postgres.tenant_session(context) as session:
            assert (await DocumentRepository(session).get(document_id, context=context)) is None

    async def test_soft_delete_is_not_repeatable(
        self, postgres: PostgresManager, context: TenantContext
    ) -> None:
        async with postgres.tenant_session(context) as session:
            document = await DocumentRepository(session).add(
                _document(context.tenant_id), context=context
            )
            document_id = document.document_id

        async with postgres.tenant_session(context) as session:
            assert await DocumentRepository(session).soft_delete(document_id, context=context)
        async with postgres.tenant_session(context) as session:
            assert not await DocumentRepository(session).soft_delete(document_id, context=context)


class TestChunkRepository:
    async def _seed_document(self, postgres: PostgresManager, context: TenantContext) -> uuid.UUID:
        async with postgres.tenant_session(context) as session:
            document = await DocumentRepository(session).add(
                _document(context.tenant_id), context=context
            )
            return document.document_id

    async def test_replace_persists_chunks(
        self, postgres: PostgresManager, context: TenantContext
    ) -> None:
        document_id = await self._seed_document(postgres, context)

        async with postgres.tenant_session(context) as session:
            count = await ChunkRepository(session).replace_for_document(
                document_id, _chunks(3), context=context
            )
            assert count == 3

        async with postgres.tenant_session(context) as session:
            stored = await ChunkRepository(session).list_for_document(document_id, context=context)
            assert [chunk.chunk_index for chunk in stored] == [0, 1, 2]
            assert stored[0].content_hash

    async def test_replace_leaves_no_orphaned_chunks(
        self, postgres: PostgresManager, context: TenantContext
    ) -> None:
        """Re-chunking to fewer chunks must not leave the old tail in retrieval."""
        document_id = await self._seed_document(postgres, context)

        async with postgres.tenant_session(context) as session:
            await ChunkRepository(session).replace_for_document(
                document_id, _chunks(5), context=context
            )
        async with postgres.tenant_session(context) as session:
            await ChunkRepository(session).replace_for_document(
                document_id, _chunks(2), context=context
            )

        async with postgres.tenant_session(context) as session:
            assert (
                await ChunkRepository(session).count_for_document(document_id, context=context)
            ) == 2

    async def test_chunks_do_not_leak_across_tenants(
        self,
        postgres: PostgresManager,
        context: TenantContext,
        other_context: TenantContext,
    ) -> None:
        document_id = await self._seed_document(postgres, context)
        async with postgres.tenant_session(context) as session:
            await ChunkRepository(session).replace_for_document(
                document_id, _chunks(3), context=context
            )

        async with postgres.tenant_session(other_context) as session:
            assert (
                await ChunkRepository(session).list_for_document(document_id, context=other_context)
                == []
            )

    async def test_deleting_a_document_cascades_to_chunks(
        self, postgres: PostgresManager, context: TenantContext
    ) -> None:
        document_id = await self._seed_document(postgres, context)
        async with postgres.tenant_session(context) as session:
            await ChunkRepository(session).replace_for_document(
                document_id, _chunks(3), context=context
            )

        async with postgres.tenant_session(context) as session:
            document = await DocumentRepository(session).get(document_id, context=context)
            await session.delete(document)

        async with postgres.tenant_session(context) as session:
            remaining = await session.execute(
                select(DocumentChunk).where(DocumentChunk.document_id == document_id)
            )
            assert remaining.scalars().all() == []


class TestRunAndQualityRepositories:
    async def test_run_lifecycle_is_recorded(
        self, postgres: PostgresManager, context: TenantContext
    ) -> None:
        async with postgres.tenant_session(context) as session:
            document = await DocumentRepository(session).add(
                _document(context.tenant_id), context=context
            )
            run = await IngestionRunRepository(session).start(
                document_id=document.document_id, context=context, pipeline_version="1.0.0"
            )
            document_id, run_id = document.document_id, run.run_id

        async with postgres.tenant_session(context) as session:
            await IngestionRunRepository(session).complete(
                run_id,
                context=context,
                status=IngestionStatus.CHUNKED,
                stage_durations_ms={"parse": 12.5},
                chunk_count=7,
                parser_name="text",
            )

        async with postgres.tenant_session(context) as session:
            runs = await IngestionRunRepository(session).list_for_document(
                document_id, context=context
            )
            assert runs[0].chunk_count == 7
            assert runs[0].stage_durations_ms == {"parse": 12.5}
            assert runs[0].completed_at is not None

    async def test_quality_report_is_persisted_in_full(
        self, postgres: PostgresManager, context: TenantContext
    ) -> None:
        """Full check detail is retained so thresholds can be re-evaluated later."""
        report = QualityReport(
            verdict=QualityVerdict.WARN,
            score=0.72,
            checks=(
                QualityCheck(
                    name="extraction_yield",
                    passed=True,
                    score=0.9,
                    weight=3.0,
                    detail="fine",
                    observed={"chars_per_page": 480.0},
                ),
            ),
        )
        async with postgres.tenant_session(context) as session:
            document = await DocumentRepository(session).add(
                _document(context.tenant_id), context=context
            )
            await QualityReportRepository(session).add(
                report, document_id=document.document_id, run_id=None, context=context
            )
            document_id = document.document_id

        async with postgres.tenant_session(context) as session:
            stored = await QualityReportRepository(session).latest_for_document(
                document_id, context=context
            )
            assert stored is not None
            assert stored.verdict == "warn"
            assert stored.checks["checks"][0]["observed"]["chars_per_page"] == 480.0


class TestSyncStateRepository:
    async def test_marks_targets_pending(
        self, postgres: PostgresManager, context: TenantContext
    ) -> None:
        async with postgres.tenant_session(context) as session:
            document = await DocumentRepository(session).add(
                _document(context.tenant_id), context=context
            )
            await SyncStateRepository(session).mark_pending(
                document.document_id, [SyncTarget.VECTOR, SyncTarget.NEO4J]
            )
            document_id = document.document_id

        async with postgres.tenant_session(context) as session:
            rows = (
                (
                    await session.execute(
                        select(IndexSyncState).where(IndexSyncState.document_id == document_id)
                    )
                )
                .scalars()
                .all()
            )
            assert {row.target_index for row in rows} == {"vector", "neo4j"}
            assert all(row.sync_status == "pending" for row in rows)

    async def test_reingestion_marks_existing_targets_stale(
        self, postgres: PostgresManager, context: TenantContext
    ) -> None:
        """An already-indexed document that changed is a replacement, not a new insert."""
        async with postgres.tenant_session(context) as session:
            document = await DocumentRepository(session).add(
                _document(context.tenant_id), context=context
            )
            await SyncStateRepository(session).mark_pending(
                document.document_id, [SyncTarget.VECTOR]
            )
            document_id = document.document_id

        async with postgres.tenant_session(context) as session:
            await SyncStateRepository(session).mark_pending(document_id, [SyncTarget.VECTOR])

        async with postgres.tenant_session(context) as session:
            row = (
                await session.execute(
                    select(IndexSyncState).where(IndexSyncState.document_id == document_id)
                )
            ).scalar_one()
            assert row.sync_status == "stale"


class TestAuditRepository:
    async def test_appends_and_chains_records(
        self, postgres: PostgresManager, context: TenantContext
    ) -> None:
        async with postgres.tenant_session(context) as session:
            repo = AuditRepository(session)
            for index in range(3):
                await repo.append(
                    AuditRecord(
                        tenant_id=context.tenant_id,
                        action=f"document.action_{index}",
                        resource_type="document",
                        resource_id=str(index),
                        occurred_at=dt.datetime(2026, 3, 14, 12, index, tzinfo=dt.UTC),
                    )
                )

        async with postgres.tenant_session(context) as session:
            rows = await AuditRepository(session).load_chain(context.tenant_id)
            assert len(rows) == 3
            assert rows[0].prev_hash == "0" * 64
            for previous, current in pairwise(rows):
                assert current.prev_hash == previous.row_hash

    async def test_stored_chain_verifies(
        self, postgres: PostgresManager, context: TenantContext
    ) -> None:
        records = [
            AuditRecord(
                tenant_id=context.tenant_id,
                action=f"document.action_{index}",
                resource_type="document",
                resource_id=str(index),
                occurred_at=dt.datetime(2026, 3, 14, 12, index, tzinfo=dt.UTC),
            )
            for index in range(4)
        ]
        async with postgres.tenant_session(context) as session:
            repo = AuditRepository(session)
            for record in records:
                await repo.append(record)

        async with postgres.tenant_session(context) as session:
            rows = await AuditRepository(session).load_chain(context.tenant_id)

        entries = [
            (record, row.prev_hash or "", row.row_hash)
            for record, row in zip(records, rows, strict=True)
        ]
        assert verify_chain(entries) == []

    async def test_chains_are_independent_per_tenant(
        self,
        postgres: PostgresManager,
        context: TenantContext,
        other_context: TenantContext,
    ) -> None:
        async with postgres.tenant_session(context) as session:
            await AuditRepository(session).append(
                AuditRecord(tenant_id=context.tenant_id, action="a", resource_type="document")
            )
        async with postgres.tenant_session(other_context) as session:
            await AuditRepository(session).append(
                AuditRecord(tenant_id=other_context.tenant_id, action="b", resource_type="document")
            )

        async with postgres.tenant_session(context) as session:
            repo = AuditRepository(session)
            first = await repo.load_chain(context.tenant_id)
            second = await repo.load_chain(other_context.tenant_id)

        assert len(first) == 1
        assert len(second) == 1
        assert first[0].prev_hash == "0" * 64
        assert second[0].prev_hash == "0" * 64


class TestAuditChainOrdering:
    """Regression cover: the chain must not depend on caller-supplied timestamps.

    ``audit_id`` was a random UUID and the chain was read back ordered by
    ``(occurred_at, audit_id)``. Records sharing a timestamp — routine when a batch writes
    several events in the same instant, or when a caller passes an explicit
    ``occurred_at`` — therefore had a nondeterministic read order, and ``verify_chain``
    could report tampering on a perfectly intact chain. A compliance control that raises
    false alarms is a control people learn to ignore.
    """

    async def test_chain_verifies_when_records_share_a_timestamp(
        self, postgres: PostgresManager, context: TenantContext
    ) -> None:
        stamp = dt.datetime(2026, 3, 14, 12, 0, tzinfo=dt.UTC)
        records = [
            AuditRecord(
                tenant_id=context.tenant_id,
                action=f"document.action_{index}",
                resource_type="document",
                resource_id=str(index),
                occurred_at=stamp,
            )
            for index in range(8)
        ]
        async with postgres.tenant_session(context) as session:
            repo = AuditRepository(session)
            for record in records:
                await repo.append(record)

        async with postgres.tenant_session(context) as session:
            rows = await AuditRepository(session).load_chain(context.tenant_id)

        entries = [
            (record, row.prev_hash or "", row.row_hash)
            for record, row in zip(records, rows, strict=True)
        ]
        assert verify_chain(entries) == [], "identical timestamps must not break the chain"

    async def test_audit_ids_are_monotonic_in_append_order(
        self, postgres: PostgresManager, context: TenantContext
    ) -> None:
        stamp = dt.datetime(2026, 3, 14, 12, 0, tzinfo=dt.UTC)
        async with postgres.tenant_session(context) as session:
            repo = AuditRepository(session)
            for index in range(5):
                await repo.append(
                    AuditRecord(
                        tenant_id=context.tenant_id,
                        action=f"a{index}",
                        resource_type="document",
                        occurred_at=stamp,
                    )
                )

        async with postgres.tenant_session(context) as session:
            rows = await AuditRepository(session).load_chain(context.tenant_id)

        ids = [row.audit_id for row in rows]
        assert ids == sorted(ids), "the chain must be ordered by a monotonic sequence"
        assert len(set(ids)) == len(ids)

    async def test_out_of_order_timestamps_do_not_break_the_chain(
        self, postgres: PostgresManager, context: TenantContext
    ) -> None:
        """Clock skew between writers must not corrupt the chain's linkage."""
        base = dt.datetime(2026, 3, 14, 12, 0, tzinfo=dt.UTC)
        offsets = [0, -30, 60, -10, 5]
        records = [
            AuditRecord(
                tenant_id=context.tenant_id,
                action=f"skewed_{index}",
                resource_type="document",
                occurred_at=base + dt.timedelta(seconds=offset),
            )
            for index, offset in enumerate(offsets)
        ]
        async with postgres.tenant_session(context) as session:
            repo = AuditRepository(session)
            for record in records:
                await repo.append(record)

        async with postgres.tenant_session(context) as session:
            rows = await AuditRepository(session).load_chain(context.tenant_id)

        entries = [
            (record, row.prev_hash or "", row.row_hash)
            for record, row in zip(records, rows, strict=True)
        ]
        assert verify_chain(entries) == []
