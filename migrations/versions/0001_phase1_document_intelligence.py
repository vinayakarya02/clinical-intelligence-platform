"""Phase 1 — document intelligence schema.

Creates the subset of docs/database/postgres-schema.sql that the Phase 1 ingestion
pipeline reads or writes, plus the two tables Phase 1 adds to that design
(``ingestion_runs``, ``document_quality_reports``).

Row-Level Security is enabled here rather than deferred: enabling it later, after data
exists, means a window in which application bugs can cross tenants, and retrofitting
policies onto a populated table is exactly the migration people postpone.

Revision ID: 0001_phase1
Revises:
Create Date: 2026-08-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_phase1"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Tenant-scoped tables that receive an RLS policy keyed on ``app.tenant_id``.
_RLS_TABLES = (
    "documents",
    "document_chunks",
    "ingestion_runs",
    "document_quality_reports",
)


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS platform")
    op.execute('CREATE EXTENSION IF NOT EXISTS "pgcrypto"')

    op.create_table(
        "tenants",
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("tenant_type", sa.Text(), nullable=False),
        sa.Column(
            "isolation_mode", sa.Text(), nullable=False, server_default=sa.text("'shared_rls'")
        ),
        sa.Column("data_residency", sa.Text(), nullable=False, server_default=sa.text("'us'")),
        sa.Column(
            "deidentification_default",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'safe_harbor'"),
        ),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'active'")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "isolation_mode IN ('schema_per_tenant', 'database_per_tenant', 'shared_rls')",
            name="ck_tenants_isolation_mode_valid",
        ),
        sa.CheckConstraint(
            "status IN ('trial', 'active', 'suspended', 'offboarding', 'offboarded')",
            name="ck_tenants_status_valid",
        ),
        sa.PrimaryKeyConstraint("tenant_id", name="pk_tenants"),
        sa.UniqueConstraint("slug", name="uq_tenants_slug"),
        schema="platform",
    )

    op.create_table(
        "audit_log",
        sa.Column("audit_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_user_id", sa.Text(), nullable=True),
        sa.Column("actor_service", sa.Text(), nullable=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("resource_type", sa.Text(), nullable=False),
        sa.Column("resource_id", sa.Text(), nullable=True),
        sa.Column("phi_accessed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "request_context",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("prev_hash", sa.String(length=64), nullable=True),
        sa.Column("row_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("audit_id", name="pk_audit_log"),
        schema="platform",
    )
    op.create_index(
        "ix_audit_log_tenant_time", "audit_log", ["tenant_id", "occurred_at"], schema="platform"
    )
    op.create_index("ix_audit_log_actor", "audit_log", ["actor_user_id"], schema="platform")

    op.create_table(
        "documents",
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("patient_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("document_type", sa.Text(), nullable=False),
        sa.Column("source_system", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("original_filename", sa.Text(), nullable=True),
        sa.Column("media_type", sa.String(length=255), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("object_storage_uri", sa.Text(), nullable=False),
        sa.Column("object_storage_key", sa.Text(), nullable=False),
        sa.Column(
            "deidentification_status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'not_deidentified'"),
        ),
        sa.Column(
            "access_scope",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "ingestion_status", sa.Text(), nullable=False, server_default=sa.text("'pending'")
        ),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("language", sa.String(length=16), nullable=True),
        sa.Column("effective_date", sa.Date(), nullable=True),
        sa.Column(
            "document_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("purge_after", sa.Date(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "document_type IN ('discharge_summary', 'lab_report', 'radiology_note', "
            "'trial_protocol', 'adverse_event_report', 'literature', 'guideline', "
            "'hl7v2_message', 'fhir_bundle', 'dicom_study', 'unknown')",
            name="ck_documents_document_type_valid",
        ),
        sa.CheckConstraint(
            "ingestion_status IN ('pending', 'parsed', 'normalized', 'chunked', 'embedded', "
            "'graph_indexed', 'failed', 'quarantined')",
            name="ck_documents_ingestion_status_valid",
        ),
        sa.CheckConstraint(
            "deidentification_status IN ('not_deidentified', 'safe_harbor', "
            "'expert_determination')",
            name="ck_documents_deidentification_status_valid",
        ),
        sa.PrimaryKeyConstraint("document_id", name="pk_documents"),
        sa.UniqueConstraint(
            "tenant_id",
            "source_system",
            "content_hash",
            name="uq_documents_tenant_source_content_hash",
        ),
    )
    op.create_index("ix_documents_tenant_id", "documents", ["tenant_id"])
    op.create_index("ix_documents_tenant_status", "documents", ["tenant_id", "ingestion_status"])
    op.create_index("ix_documents_tenant_type", "documents", ["tenant_id", "document_type"])
    op.create_index("ix_documents_tenant_content_hash", "documents", ["tenant_id", "content_hash"])

    op.create_table(
        "document_chunks",
        sa.Column("chunk_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("patient_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("chunk_text", sa.Text(), nullable=False),
        sa.Column("section_type", sa.Text(), nullable=False, server_default=sa.text("'narrative'")),
        sa.Column("section_heading", sa.Text(), nullable=True),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("char_start", sa.Integer(), nullable=False),
        sa.Column("char_end", sa.Integer(), nullable=False),
        sa.Column("page_start", sa.Integer(), nullable=True),
        sa.Column("page_end", sa.Integer(), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "chunk_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "section_type IN ('narrative', 'problem_list', 'generated_medication_summary', "
            "'generated_lab_summary', 'other')",
            name="ck_document_chunks_section_type_valid",
        ),
        sa.CheckConstraint("token_count >= 0", name="ck_document_chunks_token_count_non_negative"),
        sa.CheckConstraint("char_end >= char_start", name="ck_document_chunks_char_range_ordered"),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.document_id"],
            name="fk_document_chunks_document_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("chunk_id", name="pk_document_chunks"),
        sa.UniqueConstraint(
            "document_id", "chunk_index", name="uq_document_chunks_document_chunk_index"
        ),
    )
    op.create_index("ix_document_chunks_tenant_id", "document_chunks", ["tenant_id"])
    op.create_index(
        "ix_document_chunks_tenant_document", "document_chunks", ["tenant_id", "document_id"]
    )
    op.create_index(
        "ix_document_chunks_tenant_hash", "document_chunks", ["tenant_id", "content_hash"]
    )

    op.create_table(
        "ingestion_runs",
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("failed_stage", sa.Text(), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column(
            "stage_durations_ms",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("chunk_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("parser_name", sa.Text(), nullable=True),
        sa.Column("pipeline_version", sa.String(length=32), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.document_id"],
            name="fk_ingestion_runs_document_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("run_id", name="pk_ingestion_runs"),
    )
    op.create_index("ix_ingestion_runs_tenant_id", "ingestion_runs", ["tenant_id"])
    op.create_index(
        "ix_ingestion_runs_tenant_document", "ingestion_runs", ["tenant_id", "document_id"]
    )
    op.create_index(
        "ix_ingestion_runs_tenant_started", "ingestion_runs", ["tenant_id", "started_at"]
    )

    op.create_table(
        "document_quality_reports",
        sa.Column("report_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("verdict", sa.Text(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column(
            "checks",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "verdict IN ('pass', 'warn', 'fail')", name="ck_document_quality_reports_verdict_valid"
        ),
        sa.CheckConstraint(
            "score >= 0.0 AND score <= 1.0", name="ck_document_quality_reports_score_in_range"
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.document_id"],
            name="fk_document_quality_reports_document_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["ingestion_runs.run_id"],
            name="fk_document_quality_reports_run_id",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("report_id", name="pk_document_quality_reports"),
    )
    op.create_index(
        "ix_document_quality_reports_tenant_id", "document_quality_reports", ["tenant_id"]
    )
    op.create_index(
        "ix_quality_reports_tenant_document",
        "document_quality_reports",
        ["tenant_id", "document_id"],
    )

    op.create_table(
        "index_sync_state",
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target_index", sa.Text(), nullable=False),
        sa.Column("sync_status", sa.Text(), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "target_index IN ('opensearch', 'neo4j', 'vector')",
            name="ck_index_sync_state_target_index_valid",
        ),
        sa.CheckConstraint(
            "sync_status IN ('pending', 'synced', 'failed', 'stale')",
            name="ck_index_sync_state_sync_status_valid",
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.document_id"],
            name="fk_index_sync_state_document_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("document_id", "target_index", name="pk_index_sync_state"),
    )

    # Row-Level Security. `USING` governs reads, `WITH CHECK` governs writes — without the
    # latter a caller could insert a row attributed to another tenant even though it could
    # never read it back.
    for table in _RLS_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation_{table} ON {table} "
            "USING (tenant_id = current_setting('app.tenant_id', true)::uuid) "
            "WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid)"
        )


def downgrade() -> None:
    for table in _RLS_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation_{table} ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    op.drop_table("index_sync_state")
    op.drop_table("document_quality_reports")
    op.drop_table("ingestion_runs")
    op.drop_table("document_chunks")
    op.drop_table("documents")
    op.drop_table("audit_log", schema="platform")
    op.drop_table("tenants", schema="platform")
    op.execute("DROP SCHEMA IF EXISTS platform CASCADE")
