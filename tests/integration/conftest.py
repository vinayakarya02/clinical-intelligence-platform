"""Shared fixtures for the integration suite.

Written in Phase 9 W6, after the discovery that this suite had never executed. It was green
and inert for many runs, and the reasons are worth stating because they are the design
constraints for everything below.

**The old fixture destroyed the schema it ran against.** It called
``Base.metadata.create_all`` in setup and ``drop_all`` plus ``DROP SCHEMA platform CASCADE`` in
teardown. That is coherent for a suite that owns an empty database and catastrophic for one that
runs after ``alembic upgrade``: the first test's teardown dropped the migrated tables, so every
test after it errored — and because the errors surfaced through a fixture that called
``pytest.skip``, the job stayed green.

**A skip must mean "the infrastructure is not here", never "the setup broke".** The old fixture
wrapped its entire setup in ``except Exception: pytest.skip("PostgreSQL is not reachable")``, so
a permission error, a missing migration, and a genuinely absent server were indistinguishable —
and all three were silent. The fixtures here separate them:

- server unreachable → **skip**, with the connection error quoted
- server reachable but not migrated → **fail**, loudly; that is a broken environment, not an
  absent one
- anything else → **fail**; it is a bug

**Tests clean data, never structure.** Truncating before each test gives isolation and
repeatability without touching the schema, so the suite verifies exactly what the migrations
produced rather than a schema the tests invented.

That last point is the substantive one. The old RLS tests created their *own* policy on
``documents`` before asserting on it, which tested a policy the test had just written. The
migration's policy — the one production runs — was never exercised. These fixtures leave the
migrated schema alone, so the assertions are about the real thing.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from cip_core.config import PostgresSettings, get_settings
from tests.integration.services import POSTGRES, absent, integration_enabled

#: Tables emptied before each test, most-dependent first so foreign keys never block a delete.
#: Named explicitly rather than discovered: a test that silently stopped cleaning a new table
#: would leak rows into its neighbours and fail somewhere else entirely.
TENANT_TABLES = (
    "public.document_quality_reports",
    "public.document_chunks",
    "public.ingestion_runs",
    "public.outbox_events",
    "public.documents",
)

#: Every table the migrations must have created, schema-qualified.
#:
#: Qualified because the migrations use **two** schemas: the document tables land in ``public``
#: and the control-plane tables (``tenants``, ``audit_log``, ``index_sync_state``) land in
#: ``platform``. An unqualified check against ``public`` alone reports the platform tables
#: missing on a correctly migrated database — a false "not migrated" that would fail every test
#: in the suite for a reason that has nothing to do with the code.
REQUIRED_TABLES = (
    *TENANT_TABLES,
    "platform.tenants",
    "platform.audit_log",
    "platform.index_sync_state",
)


@pytest.fixture(scope="session")
def postgres_dsn() -> str:
    settings: PostgresSettings = get_settings().postgres
    return settings.dsn()


@pytest.fixture
async def pg_engine(postgres_dsn: str) -> AsyncIterator[AsyncEngine]:
    """A connected engine against the **migrated** database.

    Skips only when the server cannot be reached. Fails when it can be reached and the schema is
    not what the migrations produce, because that is a misconfigured job rather than a missing
    dependency — and a suite that skips on a broken environment reports success for work it never
    did.
    """
    engine = create_async_engine(postgres_dsn, pool_pre_ping=True)

    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception as exc:
        await engine.dispose()
        if not integration_enabled():
            pytest.skip("set CIP_RUN_INTEGRATION=1 to run")
        absent(POSTGRES, exc, where="the configured DSN")

    # Reachable. From here every problem is a failure, not a skip.
    async with engine.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT table_schema || '.' || table_name FROM information_schema.tables "
                "WHERE table_schema IN ('public', 'platform') AND table_type = 'BASE TABLE'"
            )
        )
        present = {row[0] for row in result}

    missing = sorted(set(REQUIRED_TABLES) - present)
    if missing:
        await engine.dispose()
        pytest.fail(
            f"PostgreSQL is reachable but not migrated — missing {missing}. "
            f"Run `alembic upgrade head` before the integration suite. This is a failure and "
            f"not a skip: a suite that skips a misconfigured environment reports success for "
            f"work it never did, which is how this suite stayed green while never running."
        )

    await _truncate(engine)
    yield engine
    await engine.dispose()


async def _truncate(engine: AsyncEngine) -> None:
    """Empty the tenant tables.

    TRUNCATE rather than DELETE: it resets identity sequences too, so a test asserting on
    ordering by ``sequence_id`` sees the same numbers on every run. Repeatability matters more
    here than the microseconds.

    ``app.outbox_relay`` is set because the outbox carries a policy that would otherwise hide
    every row — TRUNCATE is not itself subject to RLS, but the session-level setting keeps this
    consistent with how the relay reads the table.
    """
    async with engine.begin() as connection:
        await connection.execute(text("SET LOCAL app.outbox_relay = 'on'"))
        await connection.execute(
            text(f"TRUNCATE {', '.join(TENANT_TABLES)} RESTART IDENTITY CASCADE")
        )


@pytest.fixture
async def pg_sessions(pg_engine: AsyncEngine):
    """A session factory over the migrated database.

    ``PostgresOutboxStore`` takes a factory rather than a session because each claim/resolve
    cycle is its own transaction, so the relay cannot be exercised without one. Built from
    ``pg_engine`` so it inherits the reachability and migration checks above.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker

    return async_sessionmaker(pg_engine, expire_on_commit=False)


@pytest.fixture
def tenant_a() -> uuid.UUID:
    return uuid.UUID("aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa")


@pytest.fixture
def tenant_b() -> uuid.UUID:
    return uuid.UUID("bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb")
