"""Phase 9 W0 — one application, and every configured backend buildable.

Separate from ``test_integration.py`` deliberately: that file already carries 53 tests, and the
Phase 9 gap analysis flagged single-file test concentration as debt. Adding fifty more to it
would be adding to the problem while documenting it.

Every test here corresponds to something W0 found or fixed.
"""

from __future__ import annotations

import pytest

from cip_gateway.container import ContainerBuilder
from cip_gateway.platform import build_platform
from cip_gateway.routes import IssueKind, platform_routes
from cip_gateway.startup import CheckStatus, validate_connectivity


class TestBackendFactories:
    """Configuration named backends that no code path could build.

    ``RedisCache`` was written in Phase 4 and instantiated nowhere; Celery and Kafka did not
    exist at all. ``PlatformSettings`` refuses the in-memory backends in a deployed environment,
    so production configuration described a system nobody had wired — and nothing caught it,
    because the settings were *coherent* even though they were not *satisfiable*.
    """

    def test_every_cache_backend_builds(self) -> None:
        from cip_platform.cache.factory import build_cache
        from cip_platform.config import CachePolicy

        assert type(build_cache(CachePolicy())).__name__ == "InMemoryCache"
        redis = build_cache(CachePolicy(backend="redis", redis_url="redis://localhost:6379/0"))
        assert type(redis).__name__ == "RedisCache"

    def test_every_queue_backend_builds(self) -> None:
        from cip_platform.config import QueuePolicy
        from cip_platform.tasks.factory import build_task_queue

        assert type(build_task_queue(QueuePolicy())).__name__ == "InMemoryTaskQueue"
        durable = build_task_queue(
            QueuePolicy(backend="redis", broker_url="redis://localhost:6379/1")
        )
        assert type(durable).__name__ == "RedisTaskQueue"

    def test_every_event_backend_builds(self) -> None:
        from cip_platform.config import PlatformSettings
        from cip_platform.events.factory import build_event_bus

        assert type(build_event_bus(PlatformSettings())).__name__ == "InMemoryEventBus"
        kafka = build_event_bus(
            PlatformSettings(events_backend="kafka", events_broker_url="localhost:9092")
        )
        assert type(kafka).__name__ == "KafkaEventBus"

    async def test_a_failed_kafka_connect_leaves_the_bus_disconnected(self, monkeypatch) -> None:
        """Regression for a W6 finding: a failed ``start()`` used to leave the producer assigned.

        ``connect`` returns early when ``_producer`` is set, so once a boot-time connection
        failed, every retry became a no-op and ``is_connected`` reported true for a producer that
        had never reached a broker. The process would then publish nothing for the rest of its
        life while reporting itself healthy — and the outbox relay would mark rows published
        against a cluster it never contacted.

        The real ``connect`` runs here; only ``AIOKafkaProducer`` is substituted, so this asserts
        on the shipped code path rather than on a reimplementation of it. No broker required,
        which is why it belongs in the unit job: this defect was reachable without one, and
        nothing had ever pointed the class at a failure.
        """
        import aiokafka

        from cip_platform.events.kafka import KafkaEventBus

        starts: list[str] = []
        stops: list[str] = []

        class _RefusesToStart:
            def __init__(self, **kwargs: object) -> None:
                self.kwargs = kwargs

            async def start(self) -> None:
                starts.append("start")
                raise OSError("broker unreachable")

            async def stop(self) -> None:
                stops.append("stop")

        monkeypatch.setattr(aiokafka, "AIOKafkaProducer", _RefusesToStart)
        bus = KafkaEventBus("localhost:9092")

        with pytest.raises(OSError):
            await bus.connect()

        assert not bus.is_connected, "a failed connect left the bus reporting itself connected"
        assert stops == ["stop"], "the half-built producer was never stopped, leaking its tasks"
        assert (await bus.health_check())["status"] == "down"

        with pytest.raises(OSError):
            await bus.connect()
        assert starts == ["start", "start"], "a retry after a failed connect was silently a no-op"

    def test_a_backend_without_its_connection_string_is_refused(self) -> None:
        """Raise rather than fall back.

        A cache that silently degrades to per-process memory produces a hit rate that falls as
        replicas are added — which is diagnosed as a capacity problem, often for a long time.
        """
        from cip_platform.cache.factory import CacheBackendError, build_cache
        from cip_platform.config import CachePolicy, QueuePolicy
        from cip_platform.tasks.factory import QueueBackendError, build_task_queue

        with pytest.raises(CacheBackendError):
            build_cache(CachePolicy(backend="redis", redis_url=""))
        with pytest.raises(QueueBackendError):
            build_task_queue(QueuePolicy(backend="redis", broker_url=""))

    def test_the_composition_root_builds_every_backing_store(self) -> None:
        """The two applications are now one.

        ``PostgresManager``, ``MongoManager``, and ``Neo4jManager`` were constructed only in
        ``cip_ingestion.api.dependencies`` — a separate FastAPI application — so the unified app
        had the container, the routes, and the startup validation, and the *other* one had the
        only working database wiring. Neither was deployable.
        """
        container = build_platform()
        report = container.start()
        assert report.ok, report.render()
        for name in ("postgres", "mongo", "neo4j", "cache", "queue", "events"):
            assert container.try_get(name) is not None, f"{name} was not built"


class TestConnectIsNotReach:
    """``connect()`` proves the configuration builds. Only a probe proves a server is there."""

    async def test_connecting_without_infrastructure_reports_success(self) -> None:
        """Not a bug — the reason the reachability probe exists.

        SQLAlchemy, motor, and the Neo4j driver all open lazily by design: a pool that dialled
        eagerly could not be constructed before its database was up. So ``connect()`` returning
        means the DSN parsed and the engine exists, and says nothing about the server.

        Found during W0 by connecting the full platform on a machine with no database at all —
        every store reported connected. A startup check that stopped there would report the
        platform ready while it could not answer a single query.
        """
        container = build_platform()
        container.start()
        opened = await container.connect()

        assert opened, "no service declared a connect hook"
        assert all(status.connected for status in opened)

    async def test_the_probe_reports_what_connect_cannot(self) -> None:
        """With no infrastructure running, reachability must fail where connect passed."""
        container = build_platform()
        container.start()
        await container.connect()
        checks = await validate_connectivity(container)

        assert checks, "nothing was probed"
        postgres = next(check for check in checks if check.name == "reachable.postgres")
        # The outcome is the assertion, not the wording. Absence surfaces either as a refused
        # connection or as a probe that exceeds its timeout, depending on how the driver's
        # retry behaves on the day — both are correct detections of the same fact.
        assert postgres.status is CheckStatus.FAILED
        assert any(word in postgres.detail for word in ("unreachable", "did not answer"))

    async def test_a_non_critical_store_degrades_rather_than_failing(self) -> None:
        """Neo4j is declared non-critical: the graph enriches retrieval and does not gate it."""
        container = build_platform()
        container.start()
        await container.connect()
        checks = await validate_connectivity(container)

        neo4j = next(check for check in checks if check.name == "reachable.neo4j")
        assert neo4j.status is CheckStatus.WARNING

    async def test_aclose_runs_in_reverse_and_tolerates_a_raising_hook(self) -> None:
        closed: list[str] = []

        async def close_base(_: object) -> None:
            closed.append("base")

        async def close_leaf(_: object) -> None:
            closed.append("leaf")
            raise RuntimeError("close failed")

        container = (
            ContainerBuilder()
            .add("base", lambda _: object(), aclose=close_base)
            .add("leaf", lambda c: c.get("base"), depends_on=("base",), aclose=close_leaf)
            .build()
        )
        container.start()
        await container.aclose()

        assert closed.index("leaf") < closed.index("base"), (
            "a connection was closed while something borrowing from it was still draining"
        )

    async def test_connect_reports_every_failure_rather_than_the_first(self) -> None:
        """An operator reading a crash loop wants every unreachable dependency at once.

        Unlike ``start()``, which aborts on the first critical construction failure, connection
        failures are environmental and usually plural — one unreachable host commonly means
        several.
        """

        async def unreachable(_: object) -> None:
            raise ConnectionError("host is down")

        container = (
            ContainerBuilder()
            .add("first", lambda _: object(), connect=unreachable)
            .add("second", lambda _: object(), connect=unreachable)
            .build()
        )
        container.start()
        results = await container.connect()

        assert len(results) == 2
        assert not any(status.connected for status in results)


class TestNoRouteIsUnanswerable:
    """The Phase 8 registry asked whether a route's *service* was registered.

    ``retrieval`` was — so four routes validated cleanly while the application answered 501,
    because no handler existed. A live service is not the same claim as an answerable route.
    """

    def test_every_declared_route_has_an_adapter(self) -> None:
        from cip_gateway.app import ADAPTERS

        issues = platform_routes().validate(build_platform(), adapters=ADAPTERS)
        assert not issues, "\n".join(issue.render() for issue in issues)

    def test_a_route_without_a_handler_is_reported(self) -> None:
        """Reproduces the Phase 8 hole exactly: registered service, absent handler."""
        from cip_gateway.app import ADAPTERS

        container = build_platform()
        container.start()
        without_retrieval = {k: v for k, v in ADAPTERS.items() if k != "retrieval"}
        issues = platform_routes().validate(container, adapters=without_retrieval)

        assert IssueKind.UNIMPLEMENTED in {issue.kind for issue in issues}
        assert any("/v1/search" in issue.detail for issue in issues)

    def test_the_adapter_map_matches_the_registry(self) -> None:
        """Every operation declared as implemented must be a route the registry knows about.

        Guards the other direction: an entry left in the map after its route was removed would
        make the check pass for a route nobody serves.
        """
        from cip_gateway.app import ADAPTERS

        declared = {
            (route.service, route.operation)
            for route in platform_routes().routes
            if not route.path.startswith("/health")
        }
        mapped = {
            (service, operation)
            for service, operations in ADAPTERS.items()
            for operation in operations
        }
        assert mapped == declared, {
            "in the map, not routed": sorted(mapped - declared),
            "routed, not in the map": sorted(declared - mapped),
        }
