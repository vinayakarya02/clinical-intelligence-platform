"""The outbox against real PostgreSQL.

The in-memory store is the reference for the ordering invariant; **this** is where the SQL is
proved. The two properties that only a real database can demonstrate:

- ``FOR UPDATE SKIP LOCKED`` gives concurrent relays disjoint rows. In-memory, a set stands in
  for the row lock and the test cannot fail for the reason production would.
- ``DISTINCT ON (partition_key)`` with the ``next_attempt_at`` filter applied *outside* the CTE
  selects the true head of each partition. Moving that filter inside is a one-line change that
  publishes events out of order, and only this test would catch it.

Runs with ``CIP_RUN_INTEGRATION=1`` against the CI Postgres service.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from cip_core.config import PostgresSettings, get_settings
from cip_platform.outbox.models import OutboxEvent, OutboxStatus
from cip_platform.outbox.postgres import PostgresOutboxStore, append_to_outbox
from cip_platform.outbox.publisher import OutboxPublisher, PublisherConfig

pytestmark = pytest.mark.integration

_CREATE = """
CREATE TABLE IF NOT EXISTS outbox_events (
    event_id UUID PRIMARY KEY,
    sequence_id BIGINT GENERATED ALWAYS AS IDENTITY NOT NULL,
    event_type TEXT NOT NULL,
    tenant_id UUID NOT NULL,
    partition_key TEXT NOT NULL,
    aggregate_type TEXT NOT NULL DEFAULT '',
    aggregate_id TEXT NOT NULL DEFAULT '',
    correlation_id TEXT NOT NULL DEFAULT '',
    traceparent TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_at TIMESTAMPTZ,
    last_error TEXT NOT NULL DEFAULT '',
    payload JSONB NOT NULL DEFAULT '{}',
    CONSTRAINT ck_outbox_status CHECK (status IN ('pending', 'published', 'dead')),
    CONSTRAINT ck_outbox_partition_key CHECK (length(partition_key) > 0)
)
"""


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    settings: PostgresSettings = get_settings().postgres
    created = create_async_engine(settings.dsn(), pool_pre_ping=True)
    try:
        async with created.begin() as connection:
            await connection.execute(text(_CREATE))
            await connection.execute(text("TRUNCATE outbox_events"))
    except Exception as exc:
        await created.dispose()
        pytest.skip(f"PostgreSQL is not reachable: {type(exc).__name__}: {exc}")

    yield created

    async with created.begin() as connection:
        await connection.execute(text("DROP TABLE IF EXISTS outbox_events"))
    await created.dispose()


@pytest.fixture
def sessions(engine: AsyncEngine) -> async_sessionmaker:
    return async_sessionmaker(bind=engine, expire_on_commit=False)


class _Broker:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, dict]] = []
        self.fail_times = 0

    async def publish_message(self, *, topic: str, key: str, value: dict) -> None:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise ConnectionError("broker unavailable")
        self.sent.append((topic, key, value))

    @property
    def ids(self) -> list[str]:
        return [v["eventId"] for _, _, v in self.sent]


async def _append(sessions: async_sessionmaker, **kwargs: object) -> OutboxEvent:
    async with sessions() as session:
        event = await append_to_outbox(
            session,
            OutboxEvent(
                event_type=kwargs.pop("event_type", "patient.admitted"),  # type: ignore[arg-type]
                tenant_id=kwargs.pop("tenant_id", uuid.uuid4()),  # type: ignore[arg-type]
                partition_key=kwargs.pop("partition_key", "p-1"),  # type: ignore[arg-type]
                payload=kwargs.pop("payload", {}),  # type: ignore[arg-type]
            ),
        )
        await session.commit()
        return event


class TestAtomicity:
    async def test_a_rolled_back_transaction_leaves_no_event(self) -> None:
        """The entire point of the pattern, checked directly.

        If the append committed independently, a consumer would act on business data that never
        existed — the dual-write, reintroduced from the other direction.
        """
        settings: PostgresSettings = get_settings().postgres
        local = create_async_engine(settings.dsn())
        factory = async_sessionmaker(bind=local, expire_on_commit=False)
        try:
            async with factory() as session:
                await append_to_outbox(
                    session,
                    OutboxEvent(event_type="e", tenant_id=uuid.uuid4(), partition_key="p"),
                )
                await session.rollback()

            async with factory() as session:
                count = (
                    await session.execute(text("SELECT count(*) FROM outbox_events"))
                ).scalar_one()
            assert count == 0, "the event survived a rolled-back transaction"
        finally:
            await local.dispose()

    async def test_appending_the_same_event_id_twice_is_idempotent(
        self, sessions: async_sessionmaker
    ) -> None:
        event = OutboxEvent(event_type="e", tenant_id=uuid.uuid4(), partition_key="p")
        for _ in range(2):
            async with sessions() as session:
                await append_to_outbox(session, event)
                await session.commit()

        async with sessions() as session:
            count = (await session.execute(text("SELECT count(*) FROM outbox_events"))).scalar_one()
        assert count == 1


class TestConcurrentPublishers:
    async def test_two_relays_never_claim_the_same_row(
        self, engine: AsyncEngine, sessions: async_sessionmaker
    ) -> None:
        """`FOR UPDATE SKIP LOCKED`, proved against a real database.

        Without `SKIP LOCKED` the second relay blocks; without `FOR UPDATE` it reads the same
        rows and every event is delivered twice. Neither failure is reproducible in memory.
        """
        tenant = uuid.uuid4()
        for i in range(40):
            await _append(sessions, tenant_id=tenant, partition_key=f"p{i}", payload={"i": i})

        broker = _Broker()
        relays = [
            OutboxPublisher(
                PostgresOutboxStore(sessions), broker, config=PublisherConfig(batch_size=40)
            )
            for _ in range(4)
        ]
        await asyncio.gather(*(relay.drain_once() for relay in relays))

        assert len(broker.ids) == len(set(broker.ids)), (
            "concurrent relays published the same event more than once"
        )

        async with sessions() as session:
            remaining = (
                await session.execute(
                    text("SELECT count(*) FROM outbox_events WHERE status = 'pending'")
                )
            ).scalar_one()
        assert remaining == 0
        assert len(broker.ids) == 40


class TestOrderingInSql:
    async def test_only_the_head_of_a_partition_is_claimed(
        self, sessions: async_sessionmaker
    ) -> None:
        tenant = uuid.uuid4()
        for i in range(3):
            await _append(sessions, tenant_id=tenant, partition_key="same", payload={"i": i})

        broker = _Broker()
        publisher = OutboxPublisher(
            PostgresOutboxStore(sessions), broker, config=PublisherConfig(batch_size=10)
        )
        for _ in range(3):
            await publisher.drain_once()

        assert [v["payload"]["i"] for _, _, v in broker.sent] == [0, 1, 2]

    async def test_a_backed_off_head_is_not_overtaken(self, sessions: async_sessionmaker) -> None:
        """The ordering bug the query is shaped to prevent.

        Moving the `next_attempt_at` filter inside the CTE would make the *second* row the head
        while the first sits in backoff, and publish them out of order. Only a real database
        exercises that query.
        """
        tenant = uuid.uuid4()
        first = await _append(sessions, tenant_id=tenant, partition_key="same", payload={"i": 0})
        await _append(sessions, tenant_id=tenant, partition_key="same", payload={"i": 1})

        # Put the head into a long backoff, as a transient failure would.
        async with sessions() as session:
            await session.execute(
                text(
                    "UPDATE outbox_events SET next_attempt_at = :later, attempts = 1 "
                    "WHERE event_id = :id"
                ),
                {
                    "later": dt.datetime.now(dt.UTC) + dt.timedelta(hours=1),
                    "id": str(first.event_id),
                },
            )
            await session.commit()

        broker = _Broker()
        publisher = OutboxPublisher(PostgresOutboxStore(sessions), broker)
        await publisher.drain_once()

        assert broker.sent == [], "an event overtook the backed-off head of its own partition"


class TestCrashRecovery:
    async def test_an_abandoned_claim_is_reclaimed(self, sessions: async_sessionmaker) -> None:
        """A relay that dies mid-batch must not strand its rows.

        The claim is a row lock rather than a status column precisely for this: a lock dies with
        the connection, while a `status = 'publishing'` column set by a dead process is a row
        nothing ever reclaims.
        """
        tenant = uuid.uuid4()
        await _append(sessions, tenant_id=tenant, partition_key="p", payload={"i": 0})
        store = PostgresOutboxStore(sessions)

        claim = store.claim(10)
        batch = await claim.__aenter__()
        assert len(batch.events) == 1
        # Simulate the process dying: abandon the context without resolving.
        await claim.__aexit__(None, None, None)

        broker = _Broker()
        assert await OutboxPublisher(store, broker).drain_once() == 1

        async with sessions() as session:
            status = (
                await session.execute(text("SELECT status FROM outbox_events LIMIT 1"))
            ).scalar_one()
        assert status == OutboxStatus.PUBLISHED.value

    async def test_replay_returns_a_dead_event(self, sessions: async_sessionmaker) -> None:
        tenant = uuid.uuid4()
        event = await _append(sessions, tenant_id=tenant, partition_key="p")
        store = PostgresOutboxStore(sessions)

        broker = _Broker()
        broker.fail_times = 99
        publisher = OutboxPublisher(
            store, broker, config=PublisherConfig(max_attempts=1, base_retry_seconds=0.0)
        )
        await publisher.drain_once()

        stats = await store.stats()
        assert stats.dead == 1

        assert await store.replay(event.event_id) is True
        broker.fail_times = 0
        assert await publisher.drain_once() == 1


class TestRelayUnderRowLevelSecurity:
    """Regression: the relay must opt into its own policy or it sees nothing.

    Found by adversarial review, not by a test. The migration enables FORCE ROW LEVEL SECURITY
    with a tenant-isolation policy keyed on `app.tenant_id`; the relay is cross-tenant and cannot
    set one. Without opting into the relay policy it claims **zero rows, forever**, and the only
    symptom is a backlog that never drains — no error, no exception, nothing in a log.

    The earlier tests in this file create the table without RLS, so none of them could catch it.
    """

    @pytest.fixture
    async def secured(self, engine: AsyncEngine) -> AsyncIterator[None]:
        async with engine.begin() as connection:
            await connection.execute(text("ALTER TABLE outbox_events ENABLE ROW LEVEL SECURITY"))
            await connection.execute(text("ALTER TABLE outbox_events FORCE ROW LEVEL SECURITY"))
            await connection.execute(
                text(
                    "CREATE POLICY tenant_isolation_outbox_events ON outbox_events "
                    "USING (tenant_id = current_setting('app.tenant_id', true)::uuid) "
                    "WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid)"
                )
            )
            await connection.execute(
                text(
                    "CREATE POLICY relay_reads_all_outbox_events ON outbox_events "
                    "FOR ALL TO PUBLIC "
                    "USING (current_setting('app.outbox_relay', true) = 'on') "
                    "WITH CHECK (current_setting('app.outbox_relay', true) = 'on')"
                )
            )
        yield
        async with engine.begin() as connection:
            await connection.execute(
                text("DROP POLICY IF EXISTS relay_reads_all_outbox_events ON outbox_events")
            )
            await connection.execute(
                text("DROP POLICY IF EXISTS tenant_isolation_outbox_events ON outbox_events")
            )
            await connection.execute(text("ALTER TABLE outbox_events DISABLE ROW LEVEL SECURITY"))

    async def test_the_relay_claims_rows_with_rls_enforced(
        self, sessions: async_sessionmaker, secured: None
    ) -> None:
        tenant = uuid.uuid4()
        async with sessions() as session:
            await session.execute(text("SELECT set_config('app.outbox_relay', 'on', false)"))
            await append_to_outbox(
                session,
                OutboxEvent(event_type="e", tenant_id=tenant, partition_key="p"),
            )
            await session.commit()

        broker = _Broker()
        published = await OutboxPublisher(PostgresOutboxStore(sessions), broker).drain_once()

        assert published == 1, (
            "the relay claimed nothing under RLS — it is not opting into its policy, and in "
            "production the backlog would never drain and nothing would report an error"
        )

    async def test_an_application_session_sees_only_its_own_tenant(
        self, sessions: async_sessionmaker, secured: None
    ) -> None:
        """The isolation the policy exists for, checked in the same conditions."""
        mine, theirs = uuid.uuid4(), uuid.uuid4()
        async with sessions() as session:
            await session.execute(text("SELECT set_config('app.outbox_relay', 'on', false)"))
            for tenant in (mine, theirs):
                await append_to_outbox(
                    session,
                    OutboxEvent(event_type="e", tenant_id=tenant, partition_key=str(tenant)),
                )
            await session.commit()

        async with sessions() as session:
            await session.execute(
                text("SELECT set_config('app.tenant_id', :tenant, false)"), {"tenant": str(mine)}
            )
            visible = (
                await session.execute(text("SELECT count(*) FROM outbox_events"))
            ).scalar_one()
        assert visible == 1, "RLS did not scope the application session to its own tenant"
