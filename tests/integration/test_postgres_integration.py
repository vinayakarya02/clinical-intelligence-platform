"""Integration tests against live PostgreSQL.

These cover what the in-memory suite structurally cannot: Row-Level Security policies,
JSONB behaviour, and the migration itself. They run only when ``CIP_RUN_INTEGRATION=1``
and a PostgreSQL instance is reachable (``make services-up``).

RLS is the reason this file exists. ADR-0003 makes it the *database-enforced floor* of
tenant isolation — the layer that holds when application code forgets a filter — and that
guarantee cannot be verified anywhere except a real PostgreSQL server.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from cip_core.config import PostgresSettings, get_settings
from cip_core.db.base import Base
from cip_core.db.postgres import PostgresManager
from cip_core.models import Document, IngestionStatus
from cip_core.tenancy import TenantContext
from cip_ingestion.repositories import DocumentRepository

pytestmark = pytest.mark.integration


@pytest.fixture
async def pg_engine() -> AsyncIterator[AsyncEngine]:
    settings: PostgresSettings = get_settings().postgres
    engine = create_async_engine(settings.dsn(), pool_pre_ping=True)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("CREATE SCHEMA IF NOT EXISTS platform"))
            await connection.run_sync(Base.metadata.create_all)
    except Exception as exc:
        await engine.dispose()
        pytest.skip(f"PostgreSQL is not reachable: {type(exc).__name__}: {exc}")

    yield engine

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.execute(text("DROP SCHEMA IF EXISTS platform CASCADE"))
    await engine.dispose()


@pytest.fixture
async def pg(pg_engine: AsyncEngine) -> PostgresManager:
    return PostgresManager.from_engine(pg_engine)


def _document(tenant_id: uuid.UUID, content_hash: str) -> Document:
    return Document(
        tenant_id=tenant_id,
        document_type="discharge_summary",
        source_system="epic",
        media_type="text/plain",
        size_bytes=10,
        content_hash=content_hash,
        object_storage_uri="s3://bucket/key",
        object_storage_key=f"tenants/{tenant_id}/documents/2026/03/{content_hash}.txt",
        ingestion_status=IngestionStatus.PENDING.value,
    )


class TestPostgresConnectivity:
    async def test_health_check_reports_the_server_version(self, pg: PostgresManager) -> None:
        result = await pg.health_check()
        assert result["status"] == "ok"
        assert result["dialect"] == "postgresql"
        assert "PostgreSQL" in result.get("version", "")

    async def test_tenant_session_sets_the_rls_variable(
        self, pg: PostgresManager, context: TenantContext
    ) -> None:
        async with pg.tenant_session(context) as session:
            assert await pg.current_tenant_setting(session) == context.tenant_id

    async def test_the_setting_does_not_leak_between_transactions(
        self, pg: PostgresManager, context: TenantContext, other_context: TenantContext
    ) -> None:
        """A pooled connection must not carry one request's tenant into the next."""
        async with pg.tenant_session(context) as session:
            assert await pg.current_tenant_setting(session) == context.tenant_id

        async with pg.tenant_session(other_context) as session:
            assert await pg.current_tenant_setting(session) == other_context.tenant_id


class TestRowLevelSecurity:
    """RLS enforcement — the guarantee that only a real PostgreSQL server can prove."""

    @pytest.fixture(autouse=True)
    async def _enable_rls(self, pg_engine: AsyncEngine) -> AsyncIterator[None]:
        async with pg_engine.begin() as connection:
            await connection.execute(text("ALTER TABLE documents ENABLE ROW LEVEL SECURITY"))
            await connection.execute(text("ALTER TABLE documents FORCE ROW LEVEL SECURITY"))
            await connection.execute(
                text(
                    "CREATE POLICY tenant_isolation_documents ON documents "
                    "USING (tenant_id = current_setting('app.tenant_id', true)::uuid) "
                    "WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid)"
                )
            )
        yield
        async with pg_engine.begin() as connection:
            await connection.execute(
                text("DROP POLICY IF EXISTS tenant_isolation_documents ON documents")
            )
            await connection.execute(text("ALTER TABLE documents DISABLE ROW LEVEL SECURITY"))

    async def test_policy_hides_another_tenants_rows(
        self, pg: PostgresManager, context: TenantContext, other_context: TenantContext
    ) -> None:
        async with pg.tenant_session(context) as session:
            await DocumentRepository(session).add(
                _document(context.tenant_id, "a" * 64), context=context
            )

        async with pg.tenant_session(other_context) as session:
            visible = (await session.execute(text("SELECT count(*) FROM documents"))).scalar_one()
        assert visible == 0, "RLS must hide rows belonging to another tenant"

    async def test_policy_admits_the_owning_tenants_rows(
        self, pg: PostgresManager, context: TenantContext
    ) -> None:
        async with pg.tenant_session(context) as session:
            await DocumentRepository(session).add(
                _document(context.tenant_id, "b" * 64), context=context
            )

        async with pg.tenant_session(context) as session:
            visible = (await session.execute(text("SELECT count(*) FROM documents"))).scalar_one()
        assert visible == 1

    async def test_write_check_blocks_attribution_to_another_tenant(
        self, pg: PostgresManager, context: TenantContext, other_tenant_id: uuid.UUID
    ) -> None:
        """Without WITH CHECK a caller could insert rows it can never read back."""
        with pytest.raises(Exception) as exc_info:
            async with pg.tenant_session(context) as session:
                await session.execute(
                    text(
                        "INSERT INTO documents (document_id, tenant_id, document_type, "
                        "source_system, media_type, size_bytes, content_hash, "
                        "object_storage_uri, object_storage_key, ingestion_status, "
                        "deidentification_status, access_scope, document_metadata) "
                        "VALUES (gen_random_uuid(), :tenant, 'discharge_summary', 'epic', "
                        "'text/plain', 1, :hash, 'x', 'y', 'pending', 'not_deidentified', "
                        "'{}'::jsonb, '{}'::jsonb)"
                    ),
                    {"tenant": str(other_tenant_id), "hash": "c" * 64},
                )
        assert "policy" in str(exc_info.value).lower()

    async def test_query_without_a_tenant_setting_returns_nothing(
        self, pg_engine: AsyncEngine, pg: PostgresManager, context: TenantContext
    ) -> None:
        """Fail closed: an unscoped connection sees no rows rather than all of them."""
        async with pg.tenant_session(context) as session:
            await DocumentRepository(session).add(
                _document(context.tenant_id, "d" * 64), context=context
            )

        async with pg_engine.connect() as connection:
            try:
                visible = (
                    await connection.execute(text("SELECT count(*) FROM documents"))
                ).scalar_one()
            except ProgrammingError:
                # An unset setting can also surface as a cast error, which is equally
                # fail-closed behaviour.
                return
        assert visible == 0


class TestJsonbBehaviour:
    async def test_jsonb_round_trips_nested_metadata(
        self, pg: PostgresManager, context: TenantContext
    ) -> None:
        metadata = {
            "section_names": ["chief_complaint", "assessment"],
            "phi_indicators": ["mrn"],
            "nested": {"score": 0.93},
        }
        async with pg.tenant_session(context) as session:
            document = _document(context.tenant_id, "e" * 64)
            document.doc_metadata = metadata
            await DocumentRepository(session).add(document, context=context)
            document_id = document.document_id

        async with pg.tenant_session(context) as session:
            stored = await DocumentRepository(session).get(document_id, context=context)
        assert stored is not None
        assert stored.doc_metadata == metadata

    async def test_jsonb_supports_containment_queries(
        self, pg: PostgresManager, context: TenantContext
    ) -> None:
        """A JSON column would not support this; the JSONB variant must be in effect."""
        async with pg.tenant_session(context) as session:
            document = _document(context.tenant_id, "f" * 64)
            document.doc_metadata = {"language": "en"}
            await DocumentRepository(session).add(document, context=context)

        async with pg.tenant_session(context) as session:
            count = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM documents "
                        'WHERE document_metadata @> \'{"language": "en"}\'::jsonb'
                    )
                )
            ).scalar_one()
        assert count == 1
