"""Shared test fixtures.

The persistence fixtures run against in-memory SQLite rather than mocks. That matters:
repository code, transaction boundaries, constraint violations, and rollback behaviour are
exercised as real SQL. A mocked session cannot disagree with the code under test, so it
cannot catch the bugs that actually occur here.

What SQLite cannot cover — Row-Level Security policies, JSONB operators, partitioning — is
covered by the tests marked ``integration``, which run against real PostgreSQL when
``CIP_RUN_INTEGRATION=1``.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from cip_core.config import (
    AuthSettings,
    Environment,
    IngestionSettings,
    MongoSettings,
    Settings,
    StorageSettings,
)
from cip_core.db.base import Base
from cip_core.db.postgres import PostgresManager
from cip_core.models import tables as _tables  # noqa: F401  (registers models on Base)
from cip_core.storage.local import LocalFilesystemStorage
from cip_core.tenancy import Role, TenantContext
from tests.fakes import FakeMongoManager, StubOcrEngine

pytest_plugins = ("tests.fixtures.documents",)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip integration and OCR tests unless their prerequisites are present."""
    run_integration = os.environ.get("CIP_RUN_INTEGRATION") == "1"
    skip_integration = pytest.mark.skip(reason="set CIP_RUN_INTEGRATION=1 to run")

    ocr_available = _tesseract_available()
    skip_ocr = pytest.mark.skip(reason="Tesseract binary not available on PATH")

    for item in items:
        if "integration" in item.keywords and not run_integration:
            item.add_marker(skip_integration)
        if "ocr" in item.keywords and not ocr_available:
            item.add_marker(skip_ocr)


def _tesseract_available() -> bool:
    try:
        import pytesseract

        pytesseract.get_tesseract_version()
    except Exception:
        return False
    return True


@pytest.fixture
def tenant_id() -> uuid.UUID:
    return uuid.UUID("11111111-1111-4111-8111-111111111111")


@pytest.fixture
def other_tenant_id() -> uuid.UUID:
    return uuid.UUID("22222222-2222-4222-8222-222222222222")


@pytest.fixture
def context(tenant_id: uuid.UUID) -> TenantContext:
    """A caller with read+write document scopes."""
    return TenantContext(
        tenant_id=tenant_id,
        actor_id="test-actor",
        roles=frozenset({Role.CLINICIAN}),
        scopes=frozenset({"documents:read", "documents:write"}),
        request_id="test-request",
    )


@pytest.fixture
def other_context(other_tenant_id: uuid.UUID) -> TenantContext:
    """A caller in a different tenant, for isolation assertions."""
    return TenantContext(
        tenant_id=other_tenant_id,
        actor_id="other-actor",
        roles=frozenset({Role.CLINICIAN}),
        scopes=frozenset({"documents:read", "documents:write"}),
    )


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Test settings with storage pointed at a temporary directory."""
    return Settings(
        environment=Environment.TEST,
        service_name="cip-ingestion-test",
        log_level="WARNING",
        storage=StorageSettings(backend="local", local_root=tmp_path / "storage"),
        auth=AuthSettings(enabled=True, mode="local_hs256", jwt_secret="test-secret-value"),
        mongo=MongoSettings(database="cip_test"),
        ingestion=IngestionSettings(
            chunk_target_tokens=96,
            chunk_min_tokens=16,
            chunk_max_tokens=160,
            chunk_overlap_ratio=0.1,
            ocr_enabled=False,
        ),
    )


@pytest.fixture
def storage(tmp_path: Path) -> LocalFilesystemStorage:
    return LocalFilesystemStorage(tmp_path / "storage")


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    """In-memory SQLite engine with the schema created.

    A ``StaticPool``-like single connection is required: ``:memory:`` databases are
    per-connection, so a fresh connection from the pool would see an empty schema. The
    ``platform`` schema is emulated with ``ATTACH DATABASE``, which is how SQLite resolves
    the schema-qualified names the models emit.
    """
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=None,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _attach_platform_schema(dbapi_connection, _record) -> None:  # type: ignore[no-untyped-def]
        dbapi_connection.execute("ATTACH DATABASE ':memory:' AS platform")
        # Foreign keys are off by default in SQLite; the tests rely on them to prove
        # cascade behaviour, which is a real property of the production schema.
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    yield engine
    await engine.dispose()


@pytest.fixture
async def postgres(engine: AsyncEngine) -> PostgresManager:
    """A :class:`PostgresManager` bound to the in-memory database."""
    return PostgresManager.from_engine(engine)


@pytest.fixture
def mongo() -> FakeMongoManager:
    return FakeMongoManager()


@pytest.fixture
def ocr_engine() -> StubOcrEngine:
    return StubOcrEngine()


@pytest.fixture(autouse=True)
def _reset_settings_cache() -> Iterator[None]:
    """Keep the cached settings singleton from leaking between tests."""
    from cip_core.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def clean_database(engine: AsyncEngine) -> AsyncIterator[None]:
    """Truncate all tables between tests that share an engine."""
    yield
    async with engine.begin() as connection:
        for table in reversed(Base.metadata.sorted_tables):
            await connection.execute(text(f"DELETE FROM {table.fullname}"))


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--integration-min",
        type=int,
        default=0,
        help=(
            "Fail the run unless at least this many integration tests actually executed. "
            "A suite that skips everything and reports success is worse than no suite: it is "
            "false assurance, and it stays invisible because the job is green."
        ),
    )


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Refuse a green run in which the integration suite did nothing.

    Added in Phase 9 W7 after the integration job reported success across several runs while
    skipping every test: the database was unreachable for a configuration reason, the fixture
    skipped rather than failed, and the summary read "10 skipped" under a green tick. A suite
    that skips everything and passes is worse than no suite — it is false assurance, and it
    stays invisible precisely because the job is green.

    ``pytest_sessionfinish`` rather than ``pytest_terminal_summary``: only the former can change
    the session's exit status, and a guard that prints an error while exiting 0 reproduces the
    problem it was written to prevent.
    """
    del exitstatus
    minimum = session.config.getoption("--integration-min")
    if not minimum:
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    passed = len(reporter.stats.get("passed", [])) if reporter else 0
    if passed < minimum:
        skipped = len(reporter.stats.get("skipped", [])) if reporter else 0
        print(
            f"\nERROR: {passed} test(s) executed, fewer than the required {minimum}; "
            f"{skipped} skipped. The integration suite did not run — read the skip reasons "
            f"above rather than trusting the exit status."
        )
        session.exitstatus = 1
