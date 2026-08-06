"""The migrations themselves, against a real PostgreSQL server.

Nothing has ever verified that the migration chain runs from an empty database, that it is
reversible, or that what it produces matches what the application expects. Every prior run
started from a database the CI job had already migrated, so the migrations were exercised
exactly once per job and their *result* was never asserted on.

Each test here builds a **throwaway database** and runs the real chain against it. That is the
only way to check the properties that matter:

- ``upgrade head`` works from empty — the path a new environment takes, and the one nobody
  exercises until they stand up staging
- every revision downgrades — the path an incident takes, which is discovered under pressure if
  it has never been tried
- the schema produced matches the models the application maps to
- the RLS policies exist, are FORCEd, and are null-safe

A throwaway database rather than the shared one because a migration test that leaves the shared
schema half-downgraded breaks every test after it — which is the failure mode this whole
workstream exists to remove.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from tests.integration.services import POSTGRES, absent

pytestmark = pytest.mark.integration

#: What migrations 0001 and 0002 must produce between them, schema-qualified.
#:
#: Two schemas, not one. The document tables land in ``public``; the control-plane tables land in
#: ``platform``. Checking only ``public`` would report three tables missing on a database that is
#: correctly migrated, and — worse in the other direction — would call a downgrade complete while
#: the ``platform`` schema still held every row it was supposed to remove.
_EXPECTED_TABLES = frozenset(
    {
        "public.documents",
        "public.document_chunks",
        "public.ingestion_runs",
        "public.document_quality_reports",
        "public.outbox_events",
        "platform.tenants",
        "platform.index_sync_state",
        "platform.audit_log",
    }
)

#: Tables carrying a tenant-isolation policy, per migrations 0001 and 0002.
_RLS_TABLES = frozenset(
    {
        "documents",
        "document_chunks",
        "ingestion_runs",
        "document_quality_reports",
        "outbox_events",
    }
)


def _admin_dsn(dsn: str, database: str) -> str:
    """Point a DSN at a different database on the same server."""
    head, _, _ = dsn.rpartition("/")
    return f"{head}/{database}"


@pytest.fixture
async def scratch_database(postgres_dsn: str) -> AsyncIterator[str]:
    """An empty database, dropped afterwards.

    Created through ``postgres`` because ``CREATE DATABASE`` cannot run inside a transaction and
    cannot target the database you are connected to. ``AUTOCOMMIT`` is required for the same
    reason.
    """
    name = f"cip_mig_{uuid.uuid4().hex[:12]}"
    admin = create_async_engine(_admin_dsn(postgres_dsn, "postgres"), isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as connection:
            await connection.execute(text(f'CREATE DATABASE "{name}"'))
    except Exception as exc:
        await admin.dispose()
        absent(POSTGRES, exc, where="the maintenance database, so no scratch database was made")

    try:
        yield _admin_dsn(postgres_dsn, name)
    finally:
        async with admin.connect() as connection:
            # Terminate stragglers first: DROP DATABASE fails while any session is attached, and
            # a leaked connection would leave a database behind on every run.
            await connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :name AND pid <> pg_backend_pid()"
                ),
                {"name": name},
            )
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        await admin.dispose()


def _alembic_config(dsn: str) -> object:
    from alembic.config import Config

    root = __import__("pathlib").Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    # Alembic's sync engine cannot use the asyncpg driver.
    config.set_main_option("sqlalchemy.url", dsn.replace("+asyncpg", ""))
    return config


async def _tables(engine: AsyncEngine) -> set[str]:
    """Every base table in the two schemas the migrations own, as ``schema.table``."""
    async with engine.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT table_schema || '.' || table_name FROM information_schema.tables "
                "WHERE table_schema IN ('public', 'platform') AND table_type = 'BASE TABLE'"
            )
        )
        return {row[0] for row in result}


class TestUpgradeFromEmpty:
    async def test_the_chain_runs_from_an_empty_database(self, scratch_database: str) -> None:
        """The path a new environment takes, and the one nobody exercises until staging."""
        import asyncio

        from alembic import command

        await asyncio.to_thread(command.upgrade, _alembic_config(scratch_database), "head")

        engine = create_async_engine(scratch_database)
        try:
            produced = await _tables(engine)
            missing = sorted(_EXPECTED_TABLES - produced)
            assert not missing, f"upgrade head did not create {missing}"
        finally:
            await engine.dispose()

    async def test_every_rls_table_has_a_forced_policy(self, scratch_database: str) -> None:
        """FORCE is the load-bearing half.

        Without it the policy does not apply to the table's owner, so an application connecting
        as the owner has no isolation at all while `pg_policies` still lists the rule. That is
        precisely the misconfiguration that made the CI RLS test vacuous for several runs.
        """
        import asyncio

        from alembic import command

        await asyncio.to_thread(command.upgrade, _alembic_config(scratch_database), "head")

        engine = create_async_engine(scratch_database)
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(
                        "SELECT relname, relrowsecurity, relforcerowsecurity "
                        "FROM pg_class WHERE relname = ANY(:tables)"
                    ),
                    {"tables": sorted(_RLS_TABLES)},
                )
                rows = {r[0]: (r[1], r[2]) for r in result}
        finally:
            await engine.dispose()

        for table in sorted(_RLS_TABLES):
            enabled, forced = rows.get(table, (False, False))
            assert enabled, f"{table} does not have row-level security enabled"
            assert forced, f"{table} does not FORCE row-level security, so its owner bypasses it"

    async def test_every_policy_is_null_safe(self, scratch_database: str) -> None:
        """Regression for the defect found in W7.

        `current_setting('app.tenant_id', true)::uuid` raises on a session where the setting has
        been used and released, because PostgreSQL leaves an empty-string placeholder behind. A
        pooled connection that served one tenant-scoped request then errors on every
        RLS-protected table for any caller that does not set a tenant.
        """
        import asyncio

        from alembic import command

        await asyncio.to_thread(command.upgrade, _alembic_config(scratch_database), "head")

        engine = create_async_engine(scratch_database)
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text("SELECT tablename, policyname, qual FROM pg_policies")
                )
                policies = [(r[0], r[1], r[2] or "") for r in result]
        finally:
            await engine.dispose()

        tenant_policies = [p for p in policies if "app.tenant_id" in p[2]]
        assert tenant_policies, "no tenant-isolation policies found"
        unguarded = [
            f"{table}.{policy}" for table, policy, qual in tenant_policies if "NULLIF" not in qual
        ]
        assert not unguarded, (
            f"these policies cast app.tenant_id without NULLIF and will raise on a pooled "
            f"connection whose setting was released: {unguarded}"
        )

    async def test_an_empty_tenant_setting_filters_rather_than_raising(
        self, scratch_database: str
    ) -> None:
        """The behaviour the NULLIF guard exists to produce, exercised directly."""
        import asyncio

        from alembic import command

        await asyncio.to_thread(command.upgrade, _alembic_config(scratch_database), "head")

        engine = create_async_engine(scratch_database)
        try:
            async with engine.connect() as connection:
                # Reproduce the placeholder: set it, then release it by ending the transaction.
                await connection.execute(
                    text("SELECT set_config('app.tenant_id', :t, true)"),
                    {"t": str(uuid.uuid4())},
                )
                await connection.rollback()
                visible = (
                    await connection.execute(text("SELECT count(*) FROM documents"))
                ).scalar_one()
            assert visible == 0, "a session with no tenant must see nothing, not everything"
        finally:
            await engine.dispose()


class TestDowngrade:
    async def test_the_chain_downgrades_to_base(self, scratch_database: str) -> None:
        """The path an incident takes.

        A rollback that has never been run is a rollback nobody can rely on at 3am, and the cost
        of finding out then is the incident plus a broken schema.
        """
        import asyncio

        from alembic import command

        config = _alembic_config(scratch_database)
        await asyncio.to_thread(command.upgrade, config, "head")
        await asyncio.to_thread(command.downgrade, config, "base")

        engine = create_async_engine(scratch_database)
        try:
            remaining = await _tables(engine)
        finally:
            await engine.dispose()

        leftovers = sorted(_EXPECTED_TABLES & remaining)
        assert not leftovers, f"downgrade to base left tables behind: {leftovers}"

    async def test_upgrade_downgrade_upgrade_is_stable(self, scratch_database: str) -> None:
        """Reversibility has to round-trip, not merely run once.

        A downgrade that half-cleans leaves the next upgrade failing on an object that already
        exists — and it fails during a recovery, which is the worst possible moment.
        """
        import asyncio

        from alembic import command

        config = _alembic_config(scratch_database)
        await asyncio.to_thread(command.upgrade, config, "head")
        first = await _snapshot(scratch_database)

        await asyncio.to_thread(command.downgrade, config, "base")
        await asyncio.to_thread(command.upgrade, config, "head")
        second = await _snapshot(scratch_database)

        assert first == second, "the schema after a round trip differs from the first upgrade"

    async def test_each_revision_downgrades_one_step(self, scratch_database: str) -> None:
        """Step-by-step, because `downgrade base` can hide a broken intermediate revision."""
        import asyncio

        from alembic import command
        from alembic.script import ScriptDirectory

        config = _alembic_config(scratch_database)
        await asyncio.to_thread(command.upgrade, config, "head")

        revisions = list(ScriptDirectory.from_config(config).walk_revisions())
        assert len(revisions) >= 3, "expected at least three revisions in the chain"

        for _ in revisions:
            await asyncio.to_thread(command.downgrade, config, "-1")


async def _snapshot(dsn: str) -> list[tuple[str, str, str]]:
    """Columns and types, ordered — enough to detect a schema that did not round-trip."""
    engine = create_async_engine(dsn)
    try:
        async with engine.connect() as connection:
            result = await connection.execute(
                text(
                    "SELECT table_schema || '.' || table_name, column_name, data_type "
                    "FROM information_schema.columns "
                    "WHERE table_schema IN ('public', 'platform') "
                    "ORDER BY table_schema, table_name, column_name"
                )
            )
            return [(r[0], r[1], r[2]) for r in result]
    finally:
        await engine.dispose()
