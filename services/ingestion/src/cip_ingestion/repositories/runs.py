"""Ingestion-run and quality-report persistence."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from cip_core.models import (
    DocumentQualityReport,
    IngestionRun,
    IngestionStatus,
    SyncStatus,
    SyncTarget,
)
from cip_core.models.tables import IndexSyncState
from cip_core.tenancy import TenantContext
from cip_ingestion.processing.quality import QualityReport

__all__ = ["IngestionRunRepository", "QualityReportRepository", "SyncStateRepository"]


class IngestionRunRepository:
    """Tracks pipeline executions."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def start(
        self,
        *,
        document_id: uuid.UUID,
        context: TenantContext,
        pipeline_version: str,
    ) -> IngestionRun:
        run = IngestionRun(
            tenant_id=context.tenant_id,
            document_id=document_id,
            status=IngestionStatus.PENDING.value,
            pipeline_version=pipeline_version,
        )
        self._session.add(run)
        await self._session.flush()
        return run

    async def complete(
        self,
        run_id: uuid.UUID,
        *,
        context: TenantContext,
        status: IngestionStatus,
        stage_durations_ms: dict[str, float],
        chunk_count: int,
        parser_name: str | None = None,
        failed_stage: str | None = None,
        failure_reason: str | None = None,
    ) -> None:
        stmt = (
            update(IngestionRun)
            .where(
                IngestionRun.run_id == run_id,
                IngestionRun.tenant_id == context.tenant_id,
            )
            .values(
                status=status.value,
                stage_durations_ms=stage_durations_ms,
                chunk_count=chunk_count,
                parser_name=parser_name,
                failed_stage=failed_stage,
                failure_reason=failure_reason,
                completed_at=dt.datetime.now(dt.UTC),
            )
        )
        await self._session.execute(stmt)

    async def list_for_document(
        self, document_id: uuid.UUID, *, context: TenantContext, limit: int = 20
    ) -> Sequence[IngestionRun]:
        stmt = (
            select(IngestionRun)
            .where(
                IngestionRun.document_id == document_id,
                IngestionRun.tenant_id == context.tenant_id,
            )
            .order_by(IngestionRun.started_at.desc())
            .limit(limit)
        )
        return (await self._session.execute(stmt)).scalars().all()


class QualityReportRepository:
    """Persists data-quality assessments."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(
        self,
        report: QualityReport,
        *,
        document_id: uuid.UUID,
        run_id: uuid.UUID | None,
        context: TenantContext,
    ) -> DocumentQualityReport:
        row = DocumentQualityReport(
            tenant_id=context.tenant_id,
            document_id=document_id,
            run_id=run_id,
            verdict=str(report.verdict),
            score=report.score,
            checks=report.to_json(),
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def latest_for_document(
        self, document_id: uuid.UUID, *, context: TenantContext
    ) -> DocumentQualityReport | None:
        stmt = (
            select(DocumentQualityReport)
            .where(
                DocumentQualityReport.document_id == document_id,
                DocumentQualityReport.tenant_id == context.tenant_id,
            )
            .order_by(DocumentQualityReport.created_at.desc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()


class SyncStateRepository:
    """Watermarks for propagation to downstream indexes.

    Phase 1 only *marks work pending*; nothing consumes these rows yet. Writing them now
    means that when the vector and graph indexers arrive in Phase 2 they have an accurate
    backlog of everything ingested in the interim, instead of needing a one-off
    backfill scan over the whole corpus to discover what they missed.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def mark_pending(self, document_id: uuid.UUID, targets: Sequence[SyncTarget]) -> None:
        existing = (
            (
                await self._session.execute(
                    select(IndexSyncState.target_index).where(
                        IndexSyncState.document_id == document_id
                    )
                )
            )
            .scalars()
            .all()
        )
        known = set(existing)

        for target in targets:
            if str(target) in known:
                await self._session.execute(
                    update(IndexSyncState)
                    .where(
                        IndexSyncState.document_id == document_id,
                        IndexSyncState.target_index == str(target),
                    )
                    # STALE rather than PENDING: the document was already indexed and its
                    # content has changed, which the indexer must handle as a replacement
                    # rather than a first-time insert.
                    .values(sync_status=SyncStatus.STALE.value)
                )
            else:
                self._session.add(
                    IndexSyncState(
                        document_id=document_id,
                        target_index=str(target),
                        sync_status=SyncStatus.PENDING.value,
                    )
                )
        await self._session.flush()
