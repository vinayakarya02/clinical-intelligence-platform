"""Phase 9 W7 — make every tenant-isolation policy null-safe.

Every policy in the schema was written as::

    USING (tenant_id = current_setting('app.tenant_id', true)::uuid)

``current_setting(name, true)`` returns NULL when the setting has never existed, and NULL::uuid
is NULL, so the comparison yields NULL and the row is filtered. That is the intended behaviour
and it is what the Phase 1 tests exercise.

**It is not what happens once the setting has been used.** After a transaction-scoped
``set_config('app.tenant_id', ..., true)`` ends, PostgreSQL keeps a placeholder entry for the
customised parameter with an **empty string** value rather than removing it. From then on the
same session gets ``''`` instead of NULL, and ``''::uuid`` raises::

    invalid input syntax for type uuid: ""

So a pooled connection that served one tenant-scoped request and is then borrowed by anything
that does *not* set a tenant — a health probe, a background job, an admin query — errors on any
RLS-protected table. It presents as a database error rather than as an authorisation decision,
and it depends on connection reuse, so it appears under load and not in a fresh test.

Found by the Phase 9 outbox work, which is the first code to read an RLS-protected table from a
session that had not set a tenant. The defect is older: it is in migration 0001 and in
``docs/database/postgres-schema.sql``.

``NULLIF(..., '')`` restores the intended semantics — empty becomes NULL, NULL filters the row.

Revision ID: 0003_phase9_rls_null_safety
Revises: 0002_phase9_outbox
Create Date: 2026-08-06
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003_phase9_rls_null_safety"
down_revision: str | None = "0002_phase9_outbox"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Every table carrying a tenant-isolation policy, with the policy name used when it was created.
#: Taken from `_RLS_TABLES` in migration 0001 plus the outbox. `index_sync_state` and
#: `audit_log` carry no policy — they are not tenant-scoped in the Phase 1 design — so listing
#: them here would make this migration fail on a correct database.
_POLICIES = (
    ("documents", "tenant_isolation_documents"),
    ("document_chunks", "tenant_isolation_document_chunks"),
    ("ingestion_runs", "tenant_isolation_ingestion_runs"),
    ("document_quality_reports", "tenant_isolation_document_quality_reports"),
    ("outbox_events", "tenant_isolation_outbox_events"),
)

_SAFE = "NULLIF(current_setting('app.tenant_id', true), '')::uuid"
_UNSAFE = "current_setting('app.tenant_id', true)::uuid"


def upgrade() -> None:
    for table, policy in _POLICIES:
        # ALTER POLICY rather than DROP and CREATE: dropping leaves a window in which the table
        # has no policy at all, and on a live database that window is unrestricted cross-tenant
        # access. ALTER is atomic.
        op.execute(
            f"ALTER POLICY {policy} ON {table} "
            f"USING (tenant_id = {_SAFE}) WITH CHECK (tenant_id = {_SAFE})"
        )


def downgrade() -> None:
    for table, policy in _POLICIES:
        op.execute(
            f"ALTER POLICY {policy} ON {table} "
            f"USING (tenant_id = {_UNSAFE}) WITH CHECK (tenant_id = {_UNSAFE})"
        )
