"""The outbox against real PostgreSQL.

The in-memory store is the reference for the ordering invariant; **this** is where the SQL is
proved. The two properties that only a real database can demonstrate:

- ``FOR UPDATE SKIP LOCKED`` gives concurrent relays disjoint rows. In-memory, a set stands in
  for the row lock and the test cannot fail for the reason production would.
- ``DISTINCT ON (partition_key)`` with the ``next_attempt_at`` filter applied *outside* the CTE
  selects the true head of each partition. Moving that filter inside is a one-line change that
  publishes events out of order, and only this test would catch it.

Runs with ``CIP_RUN_INTEGRATION=1`` against the CI Postgres service.

**Rewritten in Phase 9 W6.** This file carried its own ``CREATE TABLE IF NOT EXISTS
outbox_events``, described as "a no-op once the migration has run". It is only a no-op when the
migration *did* run. When it had not, the statement quietly created the table **without row-level
security** — and every isolation assertion below then tested an unprotected table and passed. A
test that builds its own schema is a test that cannot tell you whether the real schema is right,
which is the whole reason the suite could stay green while proving nothing.

The schema now comes from ``conftest.py``, which fails rather than skips when the database is
reachable but unmigrated.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from cip_platform.outbox.models import OutboxEvent, OutboxStatus
from cip_platform.outbox.postgres import PostgresOutboxStore, append_to_outbox
from cip_platform.outbox.publisher import OutboxPublisher, PublisherConfig

pytestmark = pytest.mark.integration


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


async def _append(pg_sessions: async_sessionmaker, **kwargs: object) -> OutboxEvent:
    """Append the way application code must: inside a tenant-scoped transaction.

    The ``WITH CHECK`` half of the isolation policy refuses an insert whose ``tenant_id`` does not
    match ``app.tenant_id``, so a writer that never sets the context cannot write at all. That is
    the policy working as designed, and every production append path has to set it.
    """
    tenant = kwargs.pop("tenant_id", uuid.uuid4())
    async with pg_sessions() as session:
        await session.execute(
            text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": str(tenant)}
        )
        event = await append_to_outbox(
            session,
            OutboxEvent(
                event_type=kwargs.pop("event_type", "patient.admitted"),  # type: ignore[arg-type]
                tenant_id=tenant,  # type: ignore[arg-type]
                partition_key=kwargs.pop("partition_key", "p-1"),  # type: ignore[arg-type]
                payload=kwargs.pop("payload", {}),  # type: ignore[arg-type]
            ),
        )
        await session.commit()
        return event


class TestAtomicity:
    async def test_a_rolled_back_transaction_leaves_no_event(
        self, pg_sessions: async_sessionmaker
    ) -> None:
        """The entire point of the pattern, checked directly.

        If the append committed independently, a consumer would act on business data that never
        existed — the dual-write, reintroduced from the other direction.

        Took the shared fixture in W6. It previously built its own pg_engine, so with the database
        absent it *errored* where every neighbour skipped — the one test in the file whose
        result depended on whether Postgres happened to be running, reported as a red run that
        said nothing about the code.
        """
        tenant = uuid.uuid4()
        async with pg_sessions() as session:
            await session.execute(
                text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant)}
            )
            await append_to_outbox(
                session,
                OutboxEvent(event_type="e", tenant_id=tenant, partition_key="p"),
            )
            await session.rollback()

        async with pg_sessions() as session:
            await session.execute(text("SET LOCAL app.outbox_relay = 'on'"))
            count = (await session.execute(text("SELECT count(*) FROM outbox_events"))).scalar_one()
        assert count == 0, "the event survived a rolled-back transaction"

    async def test_appending_the_same_event_id_twice_is_idempotent(
        self, pg_sessions: async_sessionmaker
    ) -> None:
        event = OutboxEvent(event_type="e", tenant_id=uuid.uuid4(), partition_key="p")
        for _ in range(2):
            async with pg_sessions() as session:
                await append_to_outbox(session, event)
                await session.commit()

        async with pg_sessions() as session:
            await session.execute(text("SET LOCAL app.outbox_relay = 'on'"))
            count = (await session.execute(text("SELECT count(*) FROM outbox_events"))).scalar_one()
        assert count == 1


class TestConcurrentPublishers:
    async def test_two_relays_never_claim_the_same_row(
        self, pg_engine: AsyncEngine, pg_sessions: async_sessionmaker
    ) -> None:
        """`FOR UPDATE SKIP LOCKED`, proved against a real database.

        Without `SKIP LOCKED` the second relay blocks; without `FOR UPDATE` it reads the same
        rows and every event is delivered twice. Neither failure is reproducible in memory.
        """
        tenant = uuid.uuid4()
        for i in range(40):
            await _append(pg_sessions, tenant_id=tenant, partition_key=f"p{i}", payload={"i": i})

        broker = _Broker()
        relays = [
            OutboxPublisher(
                PostgresOutboxStore(pg_sessions), broker, config=PublisherConfig(batch_size=40)
            )
            for _ in range(4)
        ]
        await asyncio.gather(*(relay.drain_once() for relay in relays))

        assert len(broker.ids) == len(set(broker.ids)), (
            "concurrent relays published the same event more than once"
        )

        async with pg_sessions() as session:
            await session.execute(text("SET LOCAL app.outbox_relay = 'on'"))
            remaining = (
                await session.execute(
                    text("SELECT count(*) FROM outbox_events WHERE status = 'pending'")
                )
            ).scalar_one()
        assert remaining == 0
        assert len(broker.ids) == 40


class TestOrderingInSql:
    async def test_only_the_head_of_a_partition_is_claimed(
        self, pg_sessions: async_sessionmaker
    ) -> None:
        tenant = uuid.uuid4()
        for i in range(3):
            await _append(pg_sessions, tenant_id=tenant, partition_key="same", payload={"i": i})

        broker = _Broker()
        publisher = OutboxPublisher(
            PostgresOutboxStore(pg_sessions), broker, config=PublisherConfig(batch_size=10)
        )
        for _ in range(3):
            await publisher.drain_once()

        assert [v["payload"]["i"] for _, _, v in broker.sent] == [0, 1, 2]

    async def test_a_backed_off_head_is_not_overtaken(
        self, pg_sessions: async_sessionmaker
    ) -> None:
        """The ordering bug the query is shaped to prevent.

        Moving the `next_attempt_at` filter inside the CTE would make the *second* row the head
        while the first sits in backoff, and publish them out of order. Only a real database
        exercises that query.
        """
        tenant = uuid.uuid4()
        first = await _append(pg_sessions, tenant_id=tenant, partition_key="same", payload={"i": 0})
        await _append(pg_sessions, tenant_id=tenant, partition_key="same", payload={"i": 1})

        # Put the head into a long backoff, as a transient failure would.
        async with pg_sessions() as session:
            await session.execute(text("SET LOCAL app.outbox_relay = 'on'"))
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
        publisher = OutboxPublisher(PostgresOutboxStore(pg_sessions), broker)
        await publisher.drain_once()

        assert broker.sent == [], "an event overtook the backed-off head of its own partition"


class TestCrashRecovery:
    async def test_an_abandoned_claim_is_reclaimed(self, pg_sessions: async_sessionmaker) -> None:
        """A relay that dies mid-batch must not strand its rows.

        The claim is a row lock rather than a status column precisely for this: a lock dies with
        the connection, while a `status = 'publishing'` column set by a dead process is a row
        nothing ever reclaims.
        """
        tenant = uuid.uuid4()
        await _append(pg_sessions, tenant_id=tenant, partition_key="p", payload={"i": 0})
        store = PostgresOutboxStore(pg_sessions)

        claim = store.claim(10)
        batch = await claim.__aenter__()
        assert len(batch.events) == 1
        # Simulate the process dying: abandon the context without resolving.
        await claim.__aexit__(None, None, None)

        broker = _Broker()
        assert await OutboxPublisher(store, broker).drain_once() == 1

        async with pg_sessions() as session:
            await session.execute(text("SET LOCAL app.outbox_relay = 'on'"))
            status = (
                await session.execute(text("SELECT status FROM outbox_events LIMIT 1"))
            ).scalar_one()
        assert status == OutboxStatus.PUBLISHED.value

    async def test_replay_returns_a_dead_event(self, pg_sessions: async_sessionmaker) -> None:
        tenant = uuid.uuid4()
        event = await _append(pg_sessions, tenant_id=tenant, partition_key="p")
        store = PostgresOutboxStore(pg_sessions)

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

    Found by adversarial review rather than by a test. The migration enables FORCE ROW LEVEL
    SECURITY with a tenant-isolation policy keyed on ``app.tenant_id``; the relay is cross-tenant
    and cannot set one. Without opting into the relay policy it claims **zero rows, forever** —
    no error, no exception, just a backlog that never drains.

    These run against the migrated table, so RLS is enforced exactly as in production.
    """

    async def test_the_relay_claims_rows_with_rls_enforced(
        self, pg_sessions: async_sessionmaker
    ) -> None:
        await _append(pg_sessions, partition_key="p")

        broker = _Broker()
        published = await OutboxPublisher(PostgresOutboxStore(pg_sessions), broker).drain_once()

        assert published == 1, (
            "the relay claimed nothing under RLS — it is not opting into its policy, and in "
            "production the backlog would never drain and nothing would report an error"
        )

    async def test_an_application_session_sees_only_its_own_tenant(
        self, pg_sessions: async_sessionmaker
    ) -> None:
        """The isolation the policy exists for, checked in the same conditions."""
        mine, theirs = uuid.uuid4(), uuid.uuid4()
        for tenant in (mine, theirs):
            await _append(pg_sessions, tenant_id=tenant, partition_key=str(tenant))

        async with pg_sessions() as session:
            await session.execute(
                text("SELECT set_config('app.tenant_id', :tenant, false)"), {"tenant": str(mine)}
            )
            visible = (
                await session.execute(text("SELECT count(*) FROM outbox_events"))
            ).scalar_one()
        assert visible == 1, "RLS did not scope the application session to its own tenant"
