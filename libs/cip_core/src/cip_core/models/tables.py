"""SQLAlchemy models for the Phase 1 subset of the operational store.

These mirror docs/database/postgres-schema.sql. Only tables Phase 1 actually reads or
writes are defined; the clinical-fact tables (``patients``, ``conditions``,
``medications``, ``observations``) arrive with the Extraction & Coding workstream and are
deliberately absent rather than created empty.

One consequence of that scoping is worth flagging: ``documents.patient_id`` is a nullable
UUID with **no** foreign key in Phase 1. The target schema does declare the FK, but
enforcing it before ``patients`` exists would reject every patient-linked upload. The
constraint is added in the migration that creates ``patients``.
"""

from __future__ import annotations

import datetime as dt
import uuid
from enum import StrEnum

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from cip_core.db.base import Base
from cip_core.models.enums import (
    DeidentificationStatus,
    DocumentType,
    IngestionStatus,
    QualityVerdict,
    SectionType,
    SyncStatus,
    SyncTarget,
)

__all__ = [
    "AuditLog",
    "Document",
    "DocumentChunk",
    "DocumentQualityReport",
    "IndexSyncState",
    "IngestionRun",
    "Tenant",
]

PLATFORM_SCHEMA = "platform"

#: JSON column type. PostgreSQL gets JSONB (indexable, binary); every other dialect gets
#: portable JSON. Declaring the variant rather than hard-coding JSONB is what lets the
#: full persistence layer — repositories, transactions, constraints — be tested against
#: SQLite without a running PostgreSQL, while production still gets JSONB.
_Json = JSON().with_variant(JSONB(), "postgresql")


def _enum_check(column: str, enum: type[StrEnum], name: str) -> CheckConstraint:
    """Build a CHECK constraint from a StrEnum.

    Generating the constraint from the enum is what keeps the database and application
    definitions from drifting; see cip_core.models.enums.
    """
    values = ", ".join(f"'{member.value}'" for member in enum)
    return CheckConstraint(f"{column} IN ({values})", name=name)


class Tenant(Base):
    """Tenant registry (``platform.tenants``)."""

    __tablename__ = "tenants"
    __table_args__ = (
        CheckConstraint(
            "isolation_mode IN ('schema_per_tenant', 'database_per_tenant', 'shared_rls')",
            name="isolation_mode_valid",
        ),
        CheckConstraint(
            "status IN ('trial', 'active', 'suspended', 'offboarding', 'offboarded')",
            name="status_valid",
        ),
        {"schema": PLATFORM_SCHEMA},
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    tenant_type: Mapped[str] = mapped_column(Text, nullable=False)
    isolation_mode: Mapped[str] = mapped_column(Text, nullable=False, default="shared_rls")
    data_residency: Mapped[str] = mapped_column(Text, nullable=False, default="us")
    deidentification_default: Mapped[str] = mapped_column(
        Text, nullable=False, default=DeidentificationStatus.SAFE_HARBOR.value
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class Document(Base):
    """Ingested source document."""

    __tablename__ = "documents"
    __table_args__ = (
        # Content-addressed idempotency: re-registering identical bytes from the same
        # source system is the same document, not a second one. This is the database-level
        # backstop behind the pipeline's duplicate detection — the API's Idempotency-Key
        # handling protects against client retries, this protects against everything else.
        UniqueConstraint(
            "tenant_id", "source_system", "content_hash", name="tenant_source_content_hash"
        ),
        _enum_check("document_type", DocumentType, "document_type_valid"),
        _enum_check("ingestion_status", IngestionStatus, "ingestion_status_valid"),
        _enum_check(
            "deidentification_status", DeidentificationStatus, "deidentification_status_valid"
        ),
        Index("ix_documents_tenant_status", "tenant_id", "ingestion_status"),
        Index("ix_documents_tenant_type", "tenant_id", "document_type"),
        Index("ix_documents_tenant_content_hash", "tenant_id", "content_hash"),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    # No FK until the `patients` table lands — see module docstring.
    patient_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)

    document_type: Mapped[str] = mapped_column(Text, nullable=False)
    source_system: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    original_filename: Mapped[str | None] = mapped_column(Text, nullable=True)
    media_type: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    """Lowercase hex SHA-256 of the raw bytes. Drives storage addressing and dedup."""

    object_storage_uri: Mapped[str] = mapped_column(Text, nullable=False)
    object_storage_key: Mapped[str] = mapped_column(Text, nullable=False)

    deidentification_status: Mapped[str] = mapped_column(
        Text, nullable=False, default=DeidentificationStatus.NOT_DEIDENTIFIED.value
    )
    access_scope: Mapped[dict] = mapped_column(_Json, nullable=False, default=dict)
    ingestion_status: Mapped[str] = mapped_column(
        Text, nullable=False, default=IngestionStatus.PENDING.value
    )
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    language: Mapped[str | None] = mapped_column(String(16), nullable=True)
    effective_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    doc_metadata: Mapped[dict] = mapped_column(
        "document_metadata", _Json, nullable=False, default=dict
    )
    """Extracted document metadata. The attribute is ``doc_metadata`` because ``metadata``
    is reserved on the SQLAlchemy declarative base; the SQL column is ``document_metadata``."""

    # Soft delete + retention purge (Phase 0 review finding D3).
    deleted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    purge_after: Mapped[dt.date | None] = mapped_column(Date, nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    chunks: Mapped[list[DocumentChunk]] = relationship(
        back_populates="document", cascade="all, delete-orphan", lazy="selectin"
    )


class DocumentChunk(Base):
    """Retrieval-unit chunk derived from a document."""

    __tablename__ = "document_chunks"
    __table_args__ = (
        UniqueConstraint("document_id", "chunk_index", name="document_chunk_index"),
        _enum_check("section_type", SectionType, "section_type_valid"),
        CheckConstraint("token_count >= 0", name="token_count_non_negative"),
        CheckConstraint("char_end >= char_start", name="char_range_ordered"),
        Index("ix_document_chunks_tenant_document", "tenant_id", "document_id"),
        Index("ix_document_chunks_tenant_hash", "tenant_id", "content_hash"),
    )

    chunk_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("documents.document_id", ondelete="CASCADE"),
        nullable=False,
    )
    patient_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)

    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_text: Mapped[str] = mapped_column(Text, nullable=False)
    """PHI. Column-level encryption is applied at the application layer in the deployment
    profile described in docs/architecture/06-security-compliance.md §9."""

    section_type: Mapped[str] = mapped_column(Text, nullable=False, default=SectionType.NARRATIVE)
    section_heading: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    char_start: Mapped[int] = mapped_column(Integer, nullable=False)
    char_end: Mapped[int] = mapped_column(Integer, nullable=False)
    page_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    page_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    """Enables chunk-level dedup across revisions of the same document: an amended
    discharge summary usually differs in a few sections, and unchanged chunks keep a
    stable hash so Phase 2 can skip re-embedding them."""

    chunk_metadata: Mapped[dict] = mapped_column(_Json, nullable=False, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    document: Mapped[Document] = relationship(back_populates="chunks")


class IngestionRun(Base):
    """One execution of the ETL pipeline for one document.

    Separate from ``documents.ingestion_status`` because a document can be processed more
    than once (re-parse after an OCR upgrade, re-chunk after a strategy change) and each
    attempt needs its own stage timings and failure record for triage. The current status
    on the document answers "where is this document"; this table answers "what happened".
    """

    __tablename__ = "ingestion_runs"
    __table_args__ = (
        Index("ix_ingestion_runs_tenant_document", "tenant_id", "document_id"),
        Index("ix_ingestion_runs_tenant_started", "tenant_id", "started_at"),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("documents.document_id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    failed_stage: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    stage_durations_ms: Mapped[dict] = mapped_column(_Json, nullable=False, default=dict)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    parser_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    pipeline_version: Mapped[str] = mapped_column(String(32), nullable=False)
    """Recorded per run so a corpus can be selectively reprocessed after a pipeline
    change, instead of reprocessing everything or guessing what is stale."""

    started_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DocumentQualityReport(Base):
    """Data-quality assessment for one ingestion run."""

    __tablename__ = "document_quality_reports"
    __table_args__ = (
        _enum_check("verdict", QualityVerdict, "verdict_valid"),
        CheckConstraint("score >= 0.0 AND score <= 1.0", name="score_in_range"),
        Index("ix_quality_reports_tenant_document", "tenant_id", "document_id"),
    )

    report_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("documents.document_id", ondelete="CASCADE"),
        nullable=False,
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("ingestion_runs.run_id", ondelete="SET NULL"), nullable=True
    )
    verdict: Mapped[str] = mapped_column(Text, nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    checks: Mapped[dict] = mapped_column(_Json, nullable=False, default=dict)
    """Per-check results. Stored in full rather than only the aggregate so a threshold
    change can be evaluated against historical documents without re-running the pipeline."""

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class IndexSyncState(Base):
    """Watermark tracking propagation to downstream indexes.

    Holds no PHI (document ids and status only), which is why it carries no tenant RLS
    policy — the deliberate exception recorded in the Phase 0 review (finding D24).
    """

    __tablename__ = "index_sync_state"
    __table_args__ = (
        _enum_check("target_index", SyncTarget, "target_index_valid"),
        _enum_check("sync_status", SyncStatus, "sync_status_valid"),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("documents.document_id", ondelete="CASCADE"),
        primary_key=True,
    )
    target_index: Mapped[str] = mapped_column(Text, primary_key=True)
    sync_status: Mapped[str] = mapped_column(Text, nullable=False, default=SyncStatus.PENDING.value)
    synced_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class AuditLog(Base):
    """Append-only, hash-chained audit trail (``platform.audit_log``).

    45 CFR §164.312(b) requires an audit trail; the Phase 0 review (finding D9) required
    it be tamper-*evident*, not merely append-only by grant. ``prev_hash``/``row_hash``
    form a per-tenant chain: altering a historical row invalidates every subsequent hash,
    which a verification pass detects even if the actor could bypass RLS and grants.

    Phase 1 writes ingestion events here. The partitioning and WORM cold-tier described in
    docs/operations/sla-dr.md §3 are applied by the deployment's migration profile.
    """

    __tablename__ = "audit_log"
    __table_args__ = (
        Index("ix_audit_log_tenant_time", "tenant_id", "occurred_at"),
        Index("ix_audit_log_actor", "actor_user_id"),
        {"schema": PLATFORM_SCHEMA},
    )

    audit_id: Mapped[int] = mapped_column(
        # BigInteger everywhere except SQLite, which only auto-increments a column
        # declared exactly INTEGER PRIMARY KEY. The production DDL in the migration uses
        # PostgreSQL IDENTITY; this variant exists so the same model can be created
        # against SQLite in tests.
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    """Monotonic sequence, NOT a UUID.

    The hash chain is an ordered structure, so it needs a total order that the database
    assigns. ``occurred_at`` cannot provide it — it is supplied by the caller, ties at
    equal timestamps, and can invert under clock skew — and a random UUID provides no
    order at all. With either, two records written in the same instant could be read back
    in a different order than they were chained in, and ``verify_chain`` would report
    tampering on an intact chain: a compliance control that cries wolf gets ignored.
    """

    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    actor_user_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor_service: Mapped[str | None] = mapped_column(Text, nullable=True)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    resource_type: Mapped[str] = mapped_column(Text, nullable=False)
    resource_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    phi_accessed: Mapped[bool] = mapped_column(nullable=False, default=False)
    request_context: Mapped[dict] = mapped_column(_Json, nullable=False, default=dict)
    prev_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    row_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    occurred_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
