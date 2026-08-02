"""Document and chunk persistence.

Every method takes a :class:`~cip_core.tenancy.TenantContext` explicitly and filters on
it, even though Row-Level Security already enforces the same scope. That is deliberate
redundancy, not distrust of RLS: the tenant filter in the query keeps the repository
correct when running against a connection whose RLS session variable was not set (a
migration job, an admin console, a future service that forgets), and it makes the tenant
scope visible in the code rather than implicit in the connection.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence
from typing import Any, cast

from sqlalchemy import CursorResult, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from cip_core.logging import get_logger
from cip_core.models import Document, DocumentChunk, IngestionStatus
from cip_core.tenancy import TenantContext
from cip_ingestion.domain import TextChunk

__all__ = ["ChunkRepository", "DocumentRepository"]

_log = get_logger(__name__)


class DocumentRepository:
    """CRUD for :class:`~cip_core.models.Document`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, document: Document, *, context: TenantContext) -> Document:
        context.require_tenant(document.tenant_id)
        self._session.add(document)
        await self._session.flush()
        _log.info(
            "document.created",
            document_id=str(document.document_id),
            document_type=document.document_type,
            size_bytes=document.size_bytes,
        )
        return document

    async def get(self, document_id: uuid.UUID, *, context: TenantContext) -> Document | None:
        """Fetch a non-deleted document within the caller's tenant."""
        stmt = select(Document).where(
            Document.document_id == document_id,
            Document.tenant_id == context.tenant_id,
            Document.deleted_at.is_(None),
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def find_by_content_hash(
        self, content_hash: str, *, source_system: str, context: TenantContext
    ) -> Document | None:
        """Find an existing document with identical content from the same source system.

        Scoped by source system as well as content because the same bytes arriving from
        two systems are two facts worth recording separately — a lab result received from
        both the LIS and the EHR has different provenance, and collapsing them would lose
        that. Within one source system, identical bytes are the same document.
        """
        stmt = select(Document).where(
            Document.tenant_id == context.tenant_id,
            Document.content_hash == content_hash,
            Document.source_system == source_system,
            Document.deleted_at.is_(None),
        )
        return (await self._session.execute(stmt)).scalars().first()

    async def list_for_tenant(
        self,
        *,
        context: TenantContext,
        status: IngestionStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[Document]:
        stmt = (
            select(Document)
            .where(Document.tenant_id == context.tenant_id, Document.deleted_at.is_(None))
            .order_by(Document.created_at.desc())
            .limit(min(limit, 500))
            .offset(offset)
        )
        if status is not None:
            stmt = stmt.where(Document.ingestion_status == status.value)
        return (await self._session.execute(stmt)).scalars().all()

    async def set_status(
        self,
        document_id: uuid.UUID,
        status: IngestionStatus,
        *,
        context: TenantContext,
        failure_reason: str | None = None,
    ) -> None:
        """Update ingestion status.

        ``failure_reason`` is always written — cleared on success — so a document that
        recovers on reprocessing does not keep a stale failure message that would mislead
        the next person to read the row.
        """
        stmt = (
            update(Document)
            .where(
                Document.document_id == document_id,
                Document.tenant_id == context.tenant_id,
            )
            .values(
                ingestion_status=status.value,
                failure_reason=failure_reason,
                updated_at=dt.datetime.now(dt.UTC),
            )
        )
        await self._session.execute(stmt)

    async def apply_extraction_results(
        self,
        document_id: uuid.UUID,
        *,
        context: TenantContext,
        title: str | None,
        page_count: int | None,
        language: str | None,
        effective_date: dt.date | None,
        metadata: dict,
        document_type: str | None = None,
    ) -> None:
        """Write extracted metadata back onto the document row."""
        values: dict[str, object] = {
            "title": title,
            "page_count": page_count,
            "language": language,
            "effective_date": effective_date,
            "doc_metadata": metadata,
            "updated_at": dt.datetime.now(dt.UTC),
        }
        if document_type is not None:
            values["document_type"] = document_type

        stmt = (
            update(Document)
            .where(
                Document.document_id == document_id,
                Document.tenant_id == context.tenant_id,
            )
            .values(**values)
        )
        await self._session.execute(stmt)

    async def soft_delete(
        self, document_id: uuid.UUID, *, context: TenantContext, purge_after: dt.date | None = None
    ) -> bool:
        """Mark a document deleted and schedule its purge.

        Soft delete rather than immediate removal because the retention/purge workflow in
        docs/operations/tenant-lifecycle.md needs a window in which the deletion can be
        audited and, if it was a mistake, reversed.
        """
        stmt = (
            update(Document)
            .where(
                Document.document_id == document_id,
                Document.tenant_id == context.tenant_id,
                Document.deleted_at.is_(None),
            )
            .values(deleted_at=dt.datetime.now(dt.UTC), purge_after=purge_after)
        )
        result = await self._session.execute(stmt)
        # `rowcount` is defined on CursorResult; `Session.execute` is typed as returning
        # the narrower Result protocol, so the cast documents what UPDATE always returns.
        return bool(cast("CursorResult[Any]", result).rowcount)


class ChunkRepository:
    """Persistence for :class:`~cip_core.models.DocumentChunk`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def replace_for_document(
        self,
        document_id: uuid.UUID,
        chunks: Sequence[TextChunk],
        *,
        context: TenantContext,
        patient_id: uuid.UUID | None = None,
    ) -> int:
        """Replace all chunks for a document.

        Delete-then-insert rather than upsert-by-index: re-chunking with different
        settings produces a different number of chunks, and an upsert keyed on
        ``chunk_index`` would leave orphaned tail chunks from the previous run silently
        present in retrieval. Both statements run in the caller's transaction, so a
        failure mid-replace rolls back to the previous chunk set rather than leaving the
        document with none.
        """
        await self._session.execute(
            delete(DocumentChunk).where(
                DocumentChunk.document_id == document_id,
                DocumentChunk.tenant_id == context.tenant_id,
            )
        )

        rows = [
            DocumentChunk(
                tenant_id=context.tenant_id,
                document_id=document_id,
                patient_id=patient_id,
                chunk_index=chunk.index,
                chunk_text=chunk.text,
                section_type=str(chunk.section_type),
                section_heading=chunk.section_heading,
                token_count=chunk.token_count,
                char_start=chunk.char_start,
                char_end=chunk.char_end,
                page_start=chunk.page_start,
                page_end=chunk.page_end,
                content_hash=chunk.content_hash,
                chunk_metadata=chunk.metadata,
            )
            for chunk in chunks
        ]
        self._session.add_all(rows)
        await self._session.flush()
        _log.info("chunks.persisted", document_id=str(document_id), chunk_count=len(rows))
        return len(rows)

    async def list_for_document(
        self, document_id: uuid.UUID, *, context: TenantContext
    ) -> Sequence[DocumentChunk]:
        stmt = (
            select(DocumentChunk)
            .where(
                DocumentChunk.document_id == document_id,
                DocumentChunk.tenant_id == context.tenant_id,
            )
            .order_by(DocumentChunk.chunk_index)
        )
        return (await self._session.execute(stmt)).scalars().all()

    async def count_for_document(self, document_id: uuid.UUID, *, context: TenantContext) -> int:
        stmt = select(func.count()).where(
            DocumentChunk.document_id == document_id,
            DocumentChunk.tenant_id == context.tenant_id,
        )
        return int((await self._session.execute(stmt)).scalar_one())
