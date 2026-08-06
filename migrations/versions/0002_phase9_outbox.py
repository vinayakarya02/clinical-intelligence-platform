"""Phase 9 W7 — the transactional outbox.

One table. Every column earns its place; the indexes are where the design lives.

Row-Level Security is enabled here for the same reason Phase 1 enabled it on its tables: adding
it after data exists means a window in which a bug can cross tenants, and retrofitting a policy
onto a populated table is the migration everyone postpones. The outbox carries ``tenant_id`` on
every row precisely so the policy has something to key on — an outbox without it would be the
one table in the schema through which a tenant's events could be read by another.

Revision ID: 0002_phase9_outbox
Revises: 0001_phase1
Create Date: 2026-08-06
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_phase9_outbox"
down_revision: str | None = "0001_phase1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "outbox_events",
        # The deduplication key. Client-supplied so an application-level retry that re-runs the
        # same operation produces the same id and `ON CONFLICT DO NOTHING` absorbs it.
        sa.Column("event_id", postgresql.UUID(as_uuid=True), primary_key=True),
        # The ordering key, assigned by the database. A UUID does not order, and `created_at`
        # cannot be trusted for it: two rows written in the same millisecond on different
        # connections have no defined relative order, and clock skew across replicas makes
        # timestamps actively wrong. A bigserial is monotonic by construction.
        sa.Column("sequence_id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        # The ordering scope, and the Kafka partition key. Events sharing one are delivered in
        # sequence order; events with different keys have no relative guarantee.
        sa.Column("partition_key", sa.Text(), nullable=False),
        sa.Column("aggregate_type", sa.Text(), nullable=False, server_default=""),
        sa.Column("aggregate_id", sa.Text(), nullable=False, server_default=""),
        sa.Column("correlation_id", sa.Text(), nullable=False, server_default=""),
        # W3C trace context, so an asynchronous consumer joins the producing request's trace
        # instead of starting an orphan one.
        sa.Column("traceparent", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=False, server_default=""),
        sa.Column("payload", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.CheckConstraint("status IN ('pending', 'published', 'dead')", name="ck_outbox_status"),
        # A blank partition key would silently put every event of that kind on one partition,
        # serialising the platform's throughput behind a single consumer — and it would present
        # as a performance problem rather than as the bug it is.
        sa.CheckConstraint("length(partition_key) > 0", name="ck_outbox_partition_key"),
    )

    # The claim index. The relay's query is the only hot path on this table and it filters on
    # status, groups by partition_key, and orders by sequence_id — so this index covers it
    # exactly. Partial on 'pending' because published rows are the overwhelming majority in a
    # healthy system and indexing them would make the index grow without bound while answering
    # no query the relay asks.
    op.create_index(
        "ix_outbox_claim",
        "outbox_events",
        ["partition_key", "sequence_id"],
        postgresql_where=sa.text("status = 'pending'"),
    )
    # Backlog age, and the dead-letter listing an operator reads during an incident.
    op.create_index(
        "ix_outbox_pending_age",
        "outbox_events",
        ["created_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "ix_outbox_dead",
        "outbox_events",
        ["event_type", "created_at"],
        postgresql_where=sa.text("status = 'dead'"),
    )
    op.create_index("ix_outbox_tenant", "outbox_events", ["tenant_id", "sequence_id"])

    # Row-Level Security, matching the Phase 1 tables. FORCE so the policy applies to the table
    # owner too — without it, an application connecting as the owner bypasses the policy
    # entirely, which is the failure the Phase 9 CI work uncovered on the Phase 1 tables.
    op.execute("ALTER TABLE outbox_events ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE outbox_events FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation_outbox_events ON outbox_events
        USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
        WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid)
        """
    )
    # The relay is cross-tenant by nature: one process publishes every tenant's events, so it
    # cannot set a single app.tenant_id. It runs as a role holding BYPASSRLS rather than as the
    # application role — narrower than disabling the policy, and it keeps the application path
    # tenant-scoped where the isolation actually matters.
    op.execute(
        """
        CREATE POLICY relay_reads_all_outbox_events ON outbox_events
        FOR ALL TO PUBLIC
        USING (current_setting('app.outbox_relay', true) = 'on')
        WITH CHECK (current_setting('app.outbox_relay', true) = 'on')
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS relay_reads_all_outbox_events ON outbox_events")
    op.execute("DROP POLICY IF EXISTS tenant_isolation_outbox_events ON outbox_events")
    op.drop_index("ix_outbox_tenant", table_name="outbox_events")
    op.drop_index("ix_outbox_dead", table_name="outbox_events")
    op.drop_index("ix_outbox_pending_age", table_name="outbox_events")
    op.drop_index("ix_outbox_claim", table_name="outbox_events")
    op.drop_table("outbox_events")
