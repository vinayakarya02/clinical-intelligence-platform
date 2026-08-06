"""Integration tests against live PostgreSQL.

These cover what the in-memory suite structurally cannot: the Row-Level Security policies the
migrations create, JSONB behaviour, transaction boundaries, and concurrency. They run only when
``CIP_RUN_INTEGRATION=1`` and a PostgreSQL instance is reachable.

RLS is why this file exists. ADR-0003 makes it the *database-enforced floor* of tenant isolation
— the layer that holds when application code forgets a filter — and that guarantee cannot be
verified anywhere except a real server.

**Rewritten in Phase 9 W6.** Two things were wrong, and both meant the file proved less than it
claimed:

- Its fixture created the schema and dropped it again, including ``DROP SCHEMA platform
  CASCADE``. Run after ``alembic upgrade``, the first teardown destroyed the migrated schema and
  every subsequent test errored — reported as a skip, under a green tick.
- Its RLS tests **created their own policy** before asserting on it. They therefore tested a
  policy the test had just written, and the migration's policy — the one production runs — was
  never exercised at all.

Both are fixed by the shared fixtures in ``conftest.py``: the schema comes from the migrations,
the tests only clean data, and every assertion below is about the real policy.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine

from cip_core.db.postgres import PostgresManager
from cip_core.models import Document, IngestionStatus
from cip_core.tenancy import TenantContext
from cip_ingestion.repositories import DocumentRepository

pytestmark = pytest.mark.integration


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


class TestTheTestsThemselves:
    """Whether the conditions the rest of this file assumes actually hold.

    Every isolation assertion below is meaningful only if the connected role is subject to the
    policies. A superuser bypasses row-level security unconditionally — ``FORCE`` does not change
    that — so under one, ``test_policy_hides_another_tenants_rows`` passes on an empty table and
    proves nothing. That is not hypothetical: the CI job connected as a superuser for several
    runs and every RLS test was silently vacuous while green.

    So the premise is now asserted rather than assumed. A test suite that can be neutralised by
    the credentials it happens to be given needs to say so out loud.
    """

    async def test_the_connected_role_is_subject_to_row_level_security(
        self, pg_engine: AsyncEngine
    ) -> None:
        async with pg_engine.connect() as connection:
            row = (
                await connection.execute(
                    text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
                )
            ).one()
        assert not row.rolsuper, (
            "the suite is connected as a SUPERUSER, which bypasses every RLS policy — "
            "every isolation assertion in this file is vacuous under this role"
        )
        assert not row.rolbypassrls, (
            "the connected role has BYPASSRLS — every isolation assertion here is vacuous"
        )

    async def test_the_policies_under_test_are_forced(self, pg_engine: AsyncEngine) -> None:
        """FORCE is the half that applies the policy to the table's owner.

        The suite owns these tables, exactly as the application role does in production. Without
        FORCE, ``pg_policies`` still lists every rule while none of them applies to us.
        """
        async with pg_engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
                        "WHERE relname IN ('documents', 'document_chunks', 'outbox_events')"
                    )
                )
            ).all()
        assert rows, "none of the RLS-protected tables were found"
        for row in rows:
            assert row.relrowsecurity, f"{row.relname} does not have RLS enabled"
            assert row.relforcerowsecurity, f"{row.relname} does not FORCE RLS for its owner"


class TestRowLevelSecurity:
    """RLS enforcement — the guarantee that only a real PostgreSQL server can prove.

    Every test here runs against the policy **migration 0001 created**, hardened by migration
    0003. Nothing in this class creates, alters, or drops a policy: a test that writes the rule
    it is about to check proves only that PostgreSQL applies rules, which was never in doubt.

    """

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


class TestConcurrentDuplicateInsert:
    """Real concurrency against a real connection pool.

    The unit suite simulates the duplicate race because its in-memory SQLite fixture shares
    a single connection across tasks. Only a real PostgreSQL server with a real pool can
    demonstrate that the unique constraint is what actually arbitrates when two writers
    genuinely collide — which is the situation a bulk load creates routinely.
    """

    async def test_only_one_of_two_racing_inserts_survives(
        self, pg: PostgresManager, context: TenantContext
    ) -> None:
        import asyncio

        from sqlalchemy.exc import IntegrityError

        content_hash = "e" * 64

        async def _insert() -> str:
            try:
                async with pg.tenant_session(context) as session:
                    await DocumentRepository(session).add(
                        _document(context.tenant_id, content_hash), context=context
                    )
                return "inserted"
            except IntegrityError:
                return "conflict"

        outcomes = await asyncio.gather(_insert(), _insert(), return_exceptions=True)
        unexpected = [o for o in outcomes if isinstance(o, BaseException)]
        assert not unexpected, f"only IntegrityError is expected, got: {unexpected}"
        assert outcomes.count("inserted") == 1
        assert outcomes.count("conflict") == 1

        async with pg.tenant_session(context) as session:
            found = await DocumentRepository(session).find_by_content_hash(
                content_hash, source_system="epic", context=context
            )
        assert found is not None


class TestConnectionPoolBoundaries:
    """What a pool does under the conditions production puts it in.

    A pooled connection is reused across requests, and every one of these tests exists because
    something *carries over* when it should not — or fails to when it should.
    """

    async def test_the_tenant_setting_never_leaks_under_concurrency(
        self, pg: PostgresManager, context: TenantContext, other_context: TenantContext
    ) -> None:
        """The sequential version of this passes even if the setting is session-scoped.

        Interleaved, it does not: with a session-scoped ``set_config`` the two tenants take turns
        on the same pooled connection and each sees whatever the other set last. That is a
        cross-tenant read caused by nothing but timing, so it appears under load and never in a
        reproduction.
        """
        import asyncio

        async def observe(ctx: TenantContext) -> list[uuid.UUID | None]:
            seen: list[uuid.UUID | None] = []
            for _ in range(12):
                async with pg.tenant_session(ctx) as session:
                    seen.append(await pg.current_tenant_setting(session))
                await asyncio.sleep(0)  # yield, so the two interleave on the pool
            return seen

        mine, theirs = await asyncio.gather(observe(context), observe(other_context))

        assert set(mine) == {context.tenant_id}, f"one tenant observed another's context: {mine}"
        assert set(theirs) == {other_context.tenant_id}, (
            f"one tenant observed another's context: {theirs}"
        )

    async def test_more_concurrent_callers_than_connections_all_complete(
        self, pg: PostgresManager, context: TenantContext
    ) -> None:
        """Saturation must queue, not deadlock.

        A handler that acquires a second connection while holding the first deadlocks the moment
        demand reaches the pool size — and until then it looks perfectly healthy. Running well
        past the pool's width is the only way to find out which shape the code has.
        """
        import asyncio

        async def one(index: int) -> int:
            async with pg.tenant_session(context) as session:
                # CAST(), not `:n::int`. PostgreSQL's `::` cast collides with the `:name` bind
                # parameter syntax SQLAlchemy expands before asyncpg sees the statement, and the
                # server receives a literal colon: "syntax error at or near :".
                value = (
                    await session.execute(text("SELECT CAST(:n AS INTEGER)"), {"n": index})
                ).scalar_one()
            return int(value)

        async with asyncio.timeout(60):
            results = await asyncio.gather(*(one(i) for i in range(40)))
        assert results == list(range(40))

    async def test_a_connection_killed_mid_transaction_commits_nothing(
        self, pg: PostgresManager, context: TenantContext
    ) -> None:
        """Connection loss, caused rather than waited for.

        ``pg_terminate_backend`` reproduces what a failover, an OOM kill, or a pool timeout does
        to an in-flight transaction. The write must not be there afterwards: a partially applied
        transaction that survives a dropped connection is data corruption no retry detects,
        because the retry writes the same row again and both look correct.

        The session terminates **its own** backend. Two earlier attempts killed it from a second
        connection — first from the same pool, then from a dedicated engine — and both times the
        error surfaced on the *executioner* rather than the victim, so the test failed without
        ever reaching the assertion that matters. Self-termination removes the second connection
        and with it the ambiguity: exactly one backend is involved, it dies mid-transaction with
        an uncommitted INSERT outstanding, and the client sees precisely what a failover looks
        like.
        """
        from sqlalchemy.exc import DBAPIError

        content_hash = "9" * 64
        # DBAPIError, not Exception: a terminated backend surfaces as a driver-level error, and
        # accepting anything would let a typo in this test pass as "the connection died".
        with pytest.raises(DBAPIError):
            async with pg.tenant_session(context) as session:
                await DocumentRepository(session).add(
                    _document(context.tenant_id, content_hash), context=context
                )
                await session.flush()  # the row exists in this transaction, uncommitted
                await session.execute(text("SELECT pg_terminate_backend(pg_backend_pid())"))
                await session.execute(text("SELECT 1"))  # the connection is gone

        async with pg.tenant_session(context) as session:
            survived = await DocumentRepository(session).find_by_content_hash(
                content_hash, source_system="epic", context=context
            )
        assert survived is None, "an uncommitted row survived the loss of its connection"

    async def test_the_pool_recovers_after_a_connection_is_killed(
        self, pg: PostgresManager, context: TenantContext
    ) -> None:
        """A dropped connection must not poison the pool.

        ``pool_pre_ping`` exists so a stale connection is discarded rather than handed out. If it
        were not enabled, every checkout after a failover would fail once — and the failures
        would look like an outage lasting exactly as long as the pool's idle connections.
        """
        async with pg.tenant_session(context) as session:
            pid = (await session.execute(text("SELECT pg_backend_pid()"))).scalar_one()

        async with pg.tenant_session(context) as session:
            await session.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})

        for _ in range(3):
            async with pg.tenant_session(context) as session:
                assert (await session.execute(text("SELECT 1"))).scalar_one() == 1
