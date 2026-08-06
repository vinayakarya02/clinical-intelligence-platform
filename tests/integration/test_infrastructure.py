"""Every backing service, verified against the real thing.

Before Phase 9 W6, CI started PostgreSQL, Redis, and Neo4j — and **Redis and Neo4j were never
contacted by a single test**. Two containers started, passed their health checks, and were
billed for on every run without anything ever connecting to them. MongoDB and Kafka had no
service at all, so the Atlas vector path (ADR-0009) and the event backbone (ADR-0041) had never
executed anywhere.

Each class below connects to one service and exercises the property the platform actually
depends on, not merely that a port answers. A check that only opens a socket tells you the
container started, which is the least interesting thing about it — and the in-memory backends
already pass every test that a socket check would.

**Skips here are honest and specific.** Each names the service that is absent and what is
therefore unverified. That is the difference between "we could not check this" and the silence
that let the whole suite report success while running nothing.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import uuid

import pytest

from tests.integration.services import KAFKA, MONGO, NEO4J, REDIS, absent, unconfigured

pytestmark = pytest.mark.integration


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


# ---------------------------------------------------------------------------------------
# Redis — cache, durable task queue
# ---------------------------------------------------------------------------------------


@pytest.fixture
async def redis_client():
    url = _env("CIP_REDIS_URL")
    if not url:
        unconfigured(REDIS, "CIP_REDIS_URL")
    from cip_platform.cache.factory import build_redis_client

    client = build_redis_client(url)
    try:
        await client.ping()
    except Exception as exc:
        await client.aclose()
        absent(REDIS, exc, where=url)
    await client.flushdb()
    yield client
    await client.aclose()


class TestRedis:
    async def test_the_cache_round_trips_through_a_real_server(self, redis_client) -> None:
        from cip_platform.cache.base import CacheDomain, CacheKey
        from cip_platform.cache.redis_cache import RedisCache

        cache = RedisCache(redis_client)
        key = CacheKey.for_content(CacheDomain.RETRIEVAL, uuid.uuid4(), "a query")

        assert await cache.get(key) is None
        await cache.set(key, {"answer": 42}, ttl_seconds=60)
        assert await cache.get(key) == {"answer": 42}

    async def test_identical_content_does_not_collide_across_tenants(self, redis_client) -> None:
        """Isolation in the real key space rather than in a dict.

        The cache key is derived from a content hash, so two tenants embedding the same sentence
        produce the same hash. If the tenant is not in the key, one tenant serves the other's
        cached result — and it would look like a cache hit, not like a breach.
        """
        from cip_platform.cache.base import CacheDomain, CacheKey
        from cip_platform.cache.redis_cache import RedisCache

        cache = RedisCache(redis_client)
        mine = CacheKey.for_content(CacheDomain.EMBEDDING, uuid.uuid4(), "identical text")
        theirs = CacheKey.for_content(CacheDomain.EMBEDDING, uuid.uuid4(), "identical text")

        await cache.set(mine, {"vector": [1.0]}, ttl_seconds=60)
        assert await cache.get(theirs) is None, "identical content collided across tenants"

    async def test_namespace_invalidation_spares_other_tenants(self, redis_client) -> None:
        """`invalidate_namespace` scans a real keyspace; in memory it filters a dict."""
        from cip_platform.cache.base import CacheDomain, CacheKey
        from cip_platform.cache.redis_cache import RedisCache

        cache = RedisCache(redis_client)
        mine, theirs = uuid.uuid4(), uuid.uuid4()
        await cache.set(
            CacheKey.for_content(CacheDomain.RETRIEVAL, mine, "q"), {"n": 1}, ttl_seconds=60
        )
        theirs_key = CacheKey.for_content(CacheDomain.RETRIEVAL, theirs, "q")
        await cache.set(theirs_key, {"n": 2}, ttl_seconds=60)

        removed = await cache.invalidate_namespace(CacheDomain.RETRIEVAL, mine)

        assert removed == 1
        assert await cache.get(theirs_key) == {"n": 2}, "invalidation crossed a tenant boundary"

    async def test_a_job_enqueued_by_one_client_is_claimable_by_another(self, redis_client) -> None:
        """What "durable" means. The in-memory queue cannot fail this test; it holds a dict."""
        from cip_platform.tasks.base import JobKind, TaskSpec
        from cip_platform.tasks.redis_queue import RedisTaskQueue

        producer = RedisTaskQueue(redis_client)
        spec = TaskSpec(kind=JobKind.DOCUMENT_INGEST, tenant_id=uuid.uuid4(), payload={"n": 1})
        await producer.enqueue(spec)

        claimed = await RedisTaskQueue(redis_client).claim(spec.queue)

        assert claimed is not None, "a job enqueued by one client was invisible to another"
        assert claimed.task_id == spec.task_id
        assert claimed.payload == {"n": 1}, "the payload did not survive the round trip"

    async def test_concurrent_workers_never_claim_the_same_job(self, redis_client) -> None:
        """The Lua script's reason for existing, proved against a real server.

        Read-then-remove from Python lets two workers see the same member before either removes
        it, and the job runs twice. Against a dict that race cannot be reproduced at all, so this
        assertion is only meaningful here.
        """
        from cip_platform.tasks.base import JobKind, TaskSpec
        from cip_platform.tasks.redis_queue import RedisTaskQueue

        tenant = uuid.uuid4()
        specs = [
            TaskSpec(kind=JobKind.DOCUMENT_INGEST, tenant_id=tenant, payload={"n": n})
            for n in range(12)
        ]
        producer = RedisTaskQueue(redis_client)
        for spec in specs:
            await producer.enqueue(spec)

        queue = specs[0].queue
        workers = [RedisTaskQueue(redis_client) for _ in range(4)]
        claimed = await asyncio.gather(*(w.claim(queue) for w in workers for _ in range(4)))
        ids = [c.task_id for c in claimed if c is not None]

        assert len(ids) == len(set(ids)), (
            f"a job was claimed twice: {len(ids)} claims, {len(set(ids))} distinct"
        )
        assert len(ids) == 12, f"expected all 12 jobs claimed exactly once, got {len(ids)}"

    async def test_an_abandoned_claim_is_reclaimed(self, redis_client) -> None:
        """Worker-crash recovery.

        A worker that dies mid-job leaves the job invisible. Without reclamation it is invisible
        for ever — a document that silently never ingests, which nobody notices until somebody
        asks why it is missing.
        """
        from cip_platform.tasks.base import JobKind, TaskSpec
        from cip_platform.tasks.redis_queue import RedisTaskQueue

        now = [dt.datetime.now(dt.UTC)]
        queue = RedisTaskQueue(redis_client, visibility_timeout_seconds=60, clock=lambda: now[0])
        spec = TaskSpec(kind=JobKind.DOCUMENT_INGEST, tenant_id=uuid.uuid4())
        await queue.enqueue(spec)

        assert await queue.claim(spec.queue) is not None
        assert await queue.claim(spec.queue) is None, "a claimed job was immediately claimable"

        now[0] += dt.timedelta(seconds=120)  # the worker died; the visibility deadline passes
        assert await queue.reclaim_expired(spec.queue) == 1
        assert await queue.claim(spec.queue) is not None, "the abandoned job was never recovered"


# ---------------------------------------------------------------------------------------
# Neo4j — the clinical knowledge graph
# ---------------------------------------------------------------------------------------


@pytest.fixture
async def neo4j_manager():
    from cip_core.config import get_settings
    from cip_core.db.neo4j import Neo4jManager

    if not _env("CIP_NEO4J__URI"):
        unconfigured(NEO4J, "CIP_NEO4J__URI")

    manager = Neo4jManager(get_settings().neo4j)
    await manager.connect()
    try:
        await manager.health_check()
    except Exception as exc:
        await manager.disconnect()
        absent(NEO4J, exc)
    yield manager
    await manager.disconnect()


class TestNeo4j:
    async def test_the_health_check_reaches_the_server(self, neo4j_manager) -> None:
        detail = await neo4j_manager.health_check()
        assert detail["status"] == "ok"
        assert detail["probe"] == 1

    async def test_a_write_is_visible_to_a_separate_read_session(self, neo4j_manager) -> None:
        """Write and read are different sessions with different access modes.

        Under a cluster they can be different servers, so "I wrote it, therefore I can read it"
        is an assumption rather than a fact — and it is the assumption the graph retriever makes
        immediately after ingestion.
        """
        marker = uuid.uuid4().hex
        tenant = uuid.uuid4()
        try:
            async with neo4j_manager.write_session() as session:
                await session.run(
                    "CREATE (n:CipIntegrationProbe {tenant_id: $tenant, marker: $marker})",
                    tenant=str(tenant),
                    marker=marker,
                )
            async with neo4j_manager.read_session() as session:
                result = await session.run(
                    "MATCH (n:CipIntegrationProbe {tenant_id: $tenant, marker: $marker}) "
                    "RETURN count(n) AS n",
                    tenant=str(tenant),
                    marker=marker,
                )
                record = await result.single()
            assert record["n"] == 1, "a committed write was not visible to a read session"
        finally:
            await _delete_probe_nodes(neo4j_manager, marker)

    async def test_a_tenant_scoped_query_cannot_see_another_tenants_node(
        self, neo4j_manager
    ) -> None:
        """Neo4j has no row-level security, so isolation here is a *query-layer* property.

        That makes proving it more important than in PostgreSQL, not less: nothing beneath the
        application enforces it. In PostgreSQL a forgotten filter is caught by a policy; here a
        forgotten filter returns another tenant's clinical graph.
        """
        marker = uuid.uuid4().hex
        mine, theirs = uuid.uuid4(), uuid.uuid4()
        try:
            async with neo4j_manager.write_session() as session:
                await session.run(
                    "CREATE (n:CipIntegrationProbe {tenant_id: $tenant, marker: $marker})",
                    tenant=str(mine),
                    marker=marker,
                )
            async with neo4j_manager.read_session() as session:
                result = await session.run(
                    "MATCH (n:CipIntegrationProbe {tenant_id: $tenant, marker: $marker}) "
                    "RETURN count(n) AS n",
                    tenant=str(theirs),
                    marker=marker,
                )
                record = await result.single()
            assert record["n"] == 0, "a tenant-scoped query returned another tenant's node"
        finally:
            await _delete_probe_nodes(neo4j_manager, marker)

    async def test_a_failed_write_transaction_leaves_no_partial_graph(self, neo4j_manager) -> None:
        """Partial failure: two nodes written, then the transaction fails.

        Graph construction writes many nodes and relationships per document. If a mid-write
        failure left the earlier ones behind, the graph would accumulate fragments that no
        retry cleans up and no query distinguishes from real data — a corruption that reads as
        content. Neither node must survive.
        """
        from neo4j.exceptions import Neo4jError

        marker = uuid.uuid4().hex
        try:
            with pytest.raises(Neo4jError):
                async with neo4j_manager.write_session() as session:
                    transaction = await session.begin_transaction()
                    await transaction.run(
                        "CREATE (n:CipIntegrationProbe {marker: $marker, ord: 1})", marker=marker
                    )
                    await transaction.run(
                        "CREATE (n:CipIntegrationProbe {marker: $marker, ord: 2})", marker=marker
                    )
                    await transaction.run("THIS IS NOT CYPHER")  # the write fails part-way

            async with neo4j_manager.read_session() as session:
                result = await session.run(
                    "MATCH (n:CipIntegrationProbe {marker: $marker}) RETURN count(n) AS n",
                    marker=marker,
                )
                record = await result.single()
            assert record["n"] == 0, (
                f"{record['n']} node(s) survived a failed transaction — the graph is left "
                f"holding a fragment of a write that never completed"
            )
        finally:
            await _delete_probe_nodes(neo4j_manager, marker)


async def _delete_probe_nodes(manager, marker: str) -> None:
    async with manager.write_session() as session:
        await session.run(
            "MATCH (n:CipIntegrationProbe {marker: $marker}) DETACH DELETE n", marker=marker
        )


# ---------------------------------------------------------------------------------------
# MongoDB — parsed-document artifacts
# ---------------------------------------------------------------------------------------


@pytest.fixture
async def mongo_manager():
    from cip_core.config import get_settings
    from cip_core.db.mongo import MongoManager

    if not _env("CIP_MONGO__URI"):
        unconfigured(MONGO, "CIP_MONGO__URI")

    manager = MongoManager(get_settings().mongo)
    await manager.connect()
    try:
        await manager.health_check()
    except Exception as exc:
        await manager.disconnect()
        absent(MONGO, exc)
    yield manager
    await manager.disconnect()


class TestMongo:
    async def test_the_health_check_reaches_the_server(self, mongo_manager) -> None:
        detail = await mongo_manager.health_check()
        assert detail["status"] == "ok"
        assert detail["ping"] == 1.0

    async def test_a_parsed_document_round_trips(self, mongo_manager) -> None:
        """Nested structure included: parsed documents are deeply nested, and a driver
        misconfiguration that flattens or reorders them would corrupt every artifact."""
        tenant = uuid.uuid4()
        marker = uuid.uuid4().hex
        collection = mongo_manager.tenant_collection("cip_integration_probe", tenant)
        try:
            await collection.insert_one(
                {"marker": marker, "sections": [{"heading": "Findings", "spans": [1, 2, 3]}]}
            )
            found = await collection.find_one({"marker": marker})
            assert found is not None
            assert found["sections"][0]["spans"] == [1, 2, 3]
        finally:
            await collection.delete_many({"marker": marker})

    async def test_the_tenant_scoped_collection_hides_another_tenants_document(
        self, mongo_manager
    ) -> None:
        """`TenantScopedCollection` exists so a call site cannot forget the filter.

        Whether it works depends on the driver applying the injected field, which only a real
        server proves.
        """
        marker = uuid.uuid4().hex
        mine, theirs = uuid.uuid4(), uuid.uuid4()
        ours = mongo_manager.tenant_collection("cip_integration_probe", mine)
        others = mongo_manager.tenant_collection("cip_integration_probe", theirs)
        try:
            await ours.insert_one({"marker": marker, "body": "confidential"})

            assert await others.find_one({"marker": marker}) is None, "cross-tenant read"
            assert await others.count_documents({"marker": marker}) == 0
            assert await others.delete_many({"marker": marker}) == 0, "cross-tenant delete"
            assert await ours.find_one({"marker": marker}) is not None, "the document was destroyed"
        finally:
            await ours.delete_many({"marker": marker})

    async def test_ensure_indexes_is_idempotent(self, mongo_manager) -> None:
        """It runs on every startup, so a second call must not raise.

        The unique index on ``(tenant_id, document_id)`` is what turns a re-run of the pipeline
        into an upsert rather than duplicate artifacts, and it is created at boot.
        """
        await mongo_manager.ensure_indexes()
        await mongo_manager.ensure_indexes()

        collection = mongo_manager.database["parsed_documents"]
        names = {name async for name in _index_names(collection)}
        assert any("document_id" in name for name in names), f"unique index missing: {names}"


async def _index_names(collection):
    async for index in collection.list_indexes():
        yield index["name"]


# ---------------------------------------------------------------------------------------
# Kafka — the event backbone
# ---------------------------------------------------------------------------------------


@pytest.fixture
async def kafka_bus():
    brokers = _env("CIP_EVENTS_BROKER_URL")
    if not brokers:
        unconfigured(KAFKA, "CIP_EVENTS_BROKER_URL")
    from cip_platform.events.kafka import KafkaEventBus

    bus = KafkaEventBus(brokers, client_id="cip-integration")
    try:
        await bus.connect()
    except Exception as exc:
        absent(KAFKA, exc, where=brokers)
    yield bus
    await bus.aclose()


class TestKafka:
    async def test_the_producer_has_cluster_metadata(self, kafka_bus) -> None:
        detail = await kafka_bus.health_check()
        assert detail["status"] == "up", detail
        assert detail["brokers"] >= 1

    async def test_a_published_message_is_acknowledged_by_the_broker(self, kafka_bus) -> None:
        """`acks=all` means this returns only once the broker has committed the write.

        The outbox's whole guarantee rests on this call not lying: the relay marks the row
        published immediately afterwards, and a produce that returned early would lose the event
        in a failover with the row already marked done.
        """
        await kafka_bus.publish_message(
            topic="cip.integration-probe",
            key=str(uuid.uuid4()),
            value={"eventId": str(uuid.uuid4()), "probe": True},
        )

    async def test_an_event_publishes_through_the_bus(self, kafka_bus) -> None:
        from cip_platform.events.base import Event, EventType

        event = Event(
            type=EventType.DOCUMENT_PARSED,
            tenant_id=uuid.uuid4(),
            payload={"documentId": str(uuid.uuid4())},
            source="integration-test",
        )
        await kafka_bus.publish(event)

    async def test_publishing_to_an_unreachable_broker_fails_rather_than_silently_dropping(
        self, kafka_bus
    ) -> None:
        """The failure mode that matters more than the success one.

        A producer that swallows an unreachable broker turns every published event into a
        no-op, and the relay would mark rows published against a cluster that never received
        them. Port 9099 is closed on the runner.

        The expected error is narrowed to aiokafka's own hierarchy rather than ``Exception``:
        a bare ``Exception`` would also be satisfied by a ``TypeError`` in this test's own call,
        which would leave the real behaviour unasserted while the test still passed.
        """
        from aiokafka.errors import KafkaError

        from cip_platform.events.kafka import KafkaEventBus

        dead = KafkaEventBus("localhost:9099", client_id="cip-integration-dead")
        try:
            with pytest.raises((KafkaError, OSError, asyncio.TimeoutError)):
                await dead.connect()
                await dead.publish_message(topic="cip.nowhere", key="k", value={"v": 1})
        finally:
            await dead.aclose()


# ---------------------------------------------------------------------------------------
# The outbox relay, end to end: PostgreSQL in, Kafka out
# ---------------------------------------------------------------------------------------


class TestOutboxRelayAgainstRealInfrastructure:
    """W7's relay, against both real backends at once.

    Every W7 test ran the relay against an in-memory store and an in-memory bus. That proves the
    state machine and nothing about the two places it can actually fail: the RLS policy on
    ``outbox_events``, which hides every row from a relay that does not set ``app.outbox_relay``,
    and the produce call, which is the only thing standing between "marked published" and
    "delivered".
    """

    async def test_append_and_relay_moves_a_row_to_the_broker(
        self, pg_sessions, kafka_bus, tenant_a: uuid.UUID
    ) -> None:
        from cip_platform.outbox import OutboxEvent, OutboxPublisher
        from cip_platform.outbox.postgres import PostgresOutboxStore, append_to_outbox

        event = OutboxEvent(
            event_type="document.ingested",
            tenant_id=tenant_a,
            partition_key=str(tenant_a),
            payload={"documentId": str(uuid.uuid4())},
        )
        async with pg_sessions() as session, session.begin():
            await _set_tenant(session, tenant_a)
            await append_to_outbox(session, event)

        store = PostgresOutboxStore(pg_sessions)
        assert (await store.stats()).pending == 1, "the appended row was invisible to the relay"

        published = await OutboxPublisher(store, kafka_bus).drain_once()

        assert published == 1
        stats = await store.stats()
        assert stats.pending == 0, "a published row is still pending"
        assert stats.published == 1

    async def test_an_uncommitted_append_publishes_nothing(
        self, pg_sessions, kafka_bus, tenant_a: uuid.UUID
    ) -> None:
        """The entire point of the pattern.

        The event is written in the caller's transaction, so a rollback of the business data
        must take the event with it. If it did not, a consumer would act on a document that
        does not exist — which is the dual-write failure the outbox exists to remove.
        """
        from cip_platform.outbox import OutboxEvent, OutboxPublisher
        from cip_platform.outbox.postgres import PostgresOutboxStore, append_to_outbox

        async with pg_sessions() as session:
            await session.begin()
            await _set_tenant(session, tenant_a)
            await append_to_outbox(
                session,
                OutboxEvent(
                    event_type="document.ingested",
                    tenant_id=tenant_a,
                    partition_key=str(tenant_a),
                ),
            )
            await session.rollback()  # the business transaction failed

        store = PostgresOutboxStore(pg_sessions)
        assert (await store.stats()).pending == 0
        assert await OutboxPublisher(store, kafka_bus).drain_once() == 0

    async def test_concurrent_relays_never_publish_the_same_row_twice(
        self, pg_sessions, kafka_bus, tenant_a: uuid.UUID
    ) -> None:
        """`FOR UPDATE SKIP LOCKED`, against a real server.

        Two relays are the normal deployment — one per replica — and duplicate delivery of a
        clinical event is a duplicate downstream action. In memory this is a lock nobody
        contends; here two transactions genuinely race.
        """
        from cip_platform.outbox import OutboxEvent, OutboxPublisher
        from cip_platform.outbox.postgres import PostgresOutboxStore, append_to_outbox

        async with pg_sessions() as session, session.begin():
            await _set_tenant(session, tenant_a)
            for n in range(8):
                await append_to_outbox(
                    session,
                    OutboxEvent(
                        event_type="document.ingested",
                        tenant_id=tenant_a,
                        partition_key=f"partition-{n}",  # distinct, so all 8 are heads
                        payload={"n": n},
                    ),
                )

        store = PostgresOutboxStore(pg_sessions)
        counts = await asyncio.gather(
            *(OutboxPublisher(store, kafka_bus).drain_once() for _ in range(3))
        )

        assert sum(counts) == 8, f"expected 8 publishes across relays, got {counts}"
        stats = await store.stats()
        assert stats.published == 8
        assert stats.pending == 0

    async def test_the_relay_sees_rows_from_every_tenant(
        self, pg_sessions, kafka_bus, tenant_a: uuid.UUID, tenant_b: uuid.UUID
    ) -> None:
        """The relay is cross-tenant by nature and the tenant policy would hide everything.

        This is the blocker found in W7: without ``SET LOCAL app.outbox_relay``, ``claim`` returns
        zero rows for ever and reports a healthy empty outbox while nothing is delivered. The
        in-memory store has no policies, so only this test can catch a regression.
        """
        from cip_platform.outbox import OutboxEvent, OutboxPublisher
        from cip_platform.outbox.postgres import PostgresOutboxStore, append_to_outbox

        for tenant in (tenant_a, tenant_b):
            async with pg_sessions() as session, session.begin():
                await _set_tenant(session, tenant)
                await append_to_outbox(
                    session,
                    OutboxEvent(
                        event_type="document.ingested",
                        tenant_id=tenant,
                        partition_key=str(tenant),
                    ),
                )

        store = PostgresOutboxStore(pg_sessions)
        assert (await store.stats()).pending == 2, "the relay could not see both tenants' rows"
        assert await OutboxPublisher(store, kafka_bus).drain_once() == 2

    async def test_a_tenant_session_cannot_read_another_tenants_outbox_rows(
        self, pg_sessions, tenant_a: uuid.UUID, tenant_b: uuid.UUID
    ) -> None:
        """The other half: the relay sees everything, an application session sees one tenant.

        Needs no broker — it is a statement about the policy, not about delivery.
        """
        from sqlalchemy import text

        from cip_platform.outbox import OutboxEvent
        from cip_platform.outbox.postgres import append_to_outbox

        async with pg_sessions() as session, session.begin():
            await _set_tenant(session, tenant_a)
            await append_to_outbox(
                session,
                OutboxEvent(
                    event_type="document.ingested",
                    tenant_id=tenant_a,
                    partition_key=str(tenant_a),
                ),
            )

        async with pg_sessions() as session, session.begin():
            await _set_tenant(session, tenant_b)
            visible = (
                await session.execute(text("SELECT count(*) FROM outbox_events"))
            ).scalar_one()
        assert visible == 0, "a tenant session read another tenant's outbox rows"


async def _set_tenant(session, tenant_id: uuid.UUID) -> None:
    from sqlalchemy import text

    await session.execute(
        text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": str(tenant_id)}
    )
