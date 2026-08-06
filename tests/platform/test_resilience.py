"""Phase 9 W7 — circuit breakers and the transactional outbox.

Written adversarially. Every test here is an attempt to produce a lost event, a duplicate
business effect, an out-of-order delivery, a retry storm, or a stalled partition — the five
failures W7 exists to prevent. A test that only exercises the happy path would have passed
against every broken draft of this code.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from cip_platform.events.memory import InMemoryEventBus
from cip_platform.outbox import (
    InMemoryOutboxStore,
    OutboxEvent,
    OutboxPublisher,
    OutboxStatus,
    PublisherConfig,
)
from cip_platform.resilience import (
    BreakerConfig,
    BreakerOpenError,
    BreakerRegistry,
    BreakerState,
    CircuitBreaker,
    GuardPolicy,
    guarded,
)


class _Clock:
    """A hand-advanced clock, so reset timeouts are testable without sleeping."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _event(partition: str = "p-1", **kwargs: object) -> OutboxEvent:
    return OutboxEvent(
        event_type=kwargs.pop("event_type", "patient.admitted"),  # type: ignore[arg-type]
        tenant_id=kwargs.pop("tenant_id", uuid.uuid4()),  # type: ignore[arg-type]
        partition_key=partition,
        **kwargs,  # type: ignore[arg-type]
    )


class _Broker:
    """A broker that can be made to fail on demand."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, dict]] = []
        self.fail_times = 0
        self.error: Exception = ConnectionError("broker unavailable")
        self.hang_seconds = 0.0

    async def publish_message(self, *, topic: str, key: str, value: dict) -> None:
        if self.hang_seconds:
            await asyncio.sleep(self.hang_seconds)
        if self.fail_times > 0:
            self.fail_times -= 1
            raise self.error
        self.sent.append((topic, key, value))

    @property
    def event_ids(self) -> list[str]:
        return [value["eventId"] for _, _, value in self.sent]


# ---------------------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------------------


class TestCircuitBreaker:
    async def test_it_opens_on_the_failure_rate_not_a_single_failure(self) -> None:
        """A breaker that opens on one failure flaps; one that never opens is decoration."""
        clock = _Clock()
        breaker = CircuitBreaker(
            "dep", BreakerConfig(failure_threshold=0.5, minimum_calls=4), clock=clock
        )

        async def boom() -> None:
            raise ConnectionError("down")

        async def ok() -> str:
            return "ok"

        # Three failures and one success is 75% — over threshold, but only after minimum_calls.
        for _ in range(2):
            with pytest.raises(ConnectionError):
                await breaker.call(boom)
        assert breaker.state is BreakerState.CLOSED, "opened before minimum_calls"

        await breaker.call(ok)
        with pytest.raises(ConnectionError):
            await breaker.call(boom)
        assert breaker.state is BreakerState.OPEN

    async def test_an_open_circuit_does_not_attempt_the_call(self) -> None:
        """The point of the breaker: shed load rather than add to it."""
        clock = _Clock()
        breaker = CircuitBreaker("dep", BreakerConfig(minimum_calls=1), clock=clock)
        attempts = 0

        async def boom() -> None:
            nonlocal attempts
            attempts += 1
            raise ConnectionError("down")

        with pytest.raises(ConnectionError):
            await breaker.call(boom)
        assert breaker.state is BreakerState.OPEN

        for _ in range(10):
            with pytest.raises(BreakerOpenError):
                await breaker.call(boom)
        assert attempts == 1, "the open circuit still reached the dependency"
        assert breaker.stats.rejected == 10

    async def test_half_open_requires_consecutive_successes(self) -> None:
        """One lucky response is not recovery."""
        clock = _Clock()
        breaker = CircuitBreaker(
            "dep",
            BreakerConfig(minimum_calls=1, reset_timeout_seconds=10.0, success_threshold=3),
            clock=clock,
        )

        async def boom() -> None:
            raise ConnectionError("down")

        async def ok() -> str:
            return "ok"

        with pytest.raises(ConnectionError):
            await breaker.call(boom)
        clock.advance(11)

        await breaker.call(ok)
        assert breaker.state is BreakerState.HALF_OPEN
        await breaker.call(ok)
        assert breaker.state is BreakerState.HALF_OPEN, "closed before the threshold"
        await breaker.call(ok)
        assert breaker.state is BreakerState.CLOSED

    async def test_a_failure_while_half_open_reopens_immediately(self) -> None:
        """Without waiting for the window: the dependency has already said it is not ready."""
        clock = _Clock()
        breaker = CircuitBreaker(
            "dep", BreakerConfig(minimum_calls=1, reset_timeout_seconds=10.0), clock=clock
        )

        async def boom() -> None:
            raise ConnectionError("down")

        with pytest.raises(ConnectionError):
            await breaker.call(boom)
        clock.advance(11)
        with pytest.raises(ConnectionError):
            await breaker.call(boom)
        assert breaker.state is BreakerState.OPEN

    async def test_a_hanging_call_is_a_failure(self) -> None:
        """The failure mode that hurts most: a dependency that accepts and never answers.

        Without a per-call timeout inside the breaker, this call never records an outcome and
        the circuit never opens on it.
        """
        breaker = CircuitBreaker("dep", BreakerConfig(minimum_calls=1, call_timeout_seconds=0.05))

        async def hang() -> None:
            await asyncio.sleep(5)

        with pytest.raises(TimeoutError):
            await breaker.call(hang)
        assert breaker.state is BreakerState.OPEN
        assert breaker.stats.timeouts == 1

    async def test_half_open_admits_only_a_bounded_number_of_probes(self) -> None:
        """Unlimited probes send full load at a dependency that has just come back."""
        clock = _Clock()
        breaker = CircuitBreaker(
            "dep",
            BreakerConfig(minimum_calls=1, reset_timeout_seconds=10.0, half_open_max_calls=2),
            clock=clock,
        )
        released = asyncio.Event()

        async def boom() -> None:
            raise ConnectionError("down")

        async def slow() -> str:
            await released.wait()
            return "ok"

        with pytest.raises(ConnectionError):
            await breaker.call(boom)
        clock.advance(11)

        probes = [asyncio.create_task(breaker.call(slow)) for _ in range(2)]
        await asyncio.sleep(0)  # let both enter
        with pytest.raises(BreakerOpenError):
            await breaker.call(slow)  # third probe refused

        released.set()
        await asyncio.gather(*probes)

    async def test_breakers_are_isolated_per_dependency(self) -> None:
        """A shared breaker turns one failing dependency into an outage of everything."""
        registry = BreakerRegistry(default_config=BreakerConfig(minimum_calls=1))

        async def boom() -> None:
            raise ConnectionError("down")

        async def ok() -> str:
            return "ok"

        with pytest.raises(ConnectionError):
            await registry.call("neo4j", boom)
        assert registry.get("neo4j").state is BreakerState.OPEN
        assert await registry.call("postgres", ok) == "ok"
        assert registry.get("postgres").state is BreakerState.CLOSED
        assert registry.open_circuits() == ("neo4j",)

    async def test_manual_reset_clears_the_window(self) -> None:
        """A reset that left the pre-outage failures would reopen on the next call."""
        breaker = CircuitBreaker("dep", BreakerConfig(minimum_calls=1))

        async def boom() -> None:
            raise ConnectionError("down")

        async def ok() -> str:
            return "ok"

        with pytest.raises(ConnectionError):
            await breaker.call(boom)
        await breaker.reset()

        assert breaker.state is BreakerState.CLOSED
        assert await breaker.call(ok) == "ok"
        assert breaker.state is BreakerState.CLOSED

    async def test_health_reports_degraded_not_down(self) -> None:
        """An open circuit is one dependency unavailable, not the platform being down.

        Reporting `down` here would remove every replica from the load balancer over a degraded
        knowledge graph — the outage the breaker was supposed to prevent.
        """
        registry = BreakerRegistry(default_config=BreakerConfig(minimum_calls=1))

        async def boom() -> None:
            raise ConnectionError("down")

        assert registry.health()["status"] == "up"
        with pytest.raises(ConnectionError):
            await registry.call("neo4j", boom)
        health = registry.health()
        assert health["status"] == "degraded"
        assert health["open"] == ["neo4j"]


class TestGuard:
    async def test_a_rejected_call_is_never_retried(self) -> None:
        """Retrying a call the breaker rejected is the storm the breaker exists to stop."""
        registry = BreakerRegistry(default_config=BreakerConfig(minimum_calls=1))
        guard = guarded(
            registry,
            "dep",
            policy=GuardPolicy(max_attempts=5, retry_on=(ConnectionError,), jitter=False),
        )
        attempts = 0

        async def boom() -> None:
            nonlocal attempts
            attempts += 1
            raise ConnectionError("down")

        with pytest.raises(ConnectionError):
            await guard.call(boom)
        before = attempts

        with pytest.raises(BreakerOpenError):
            await guard.call(boom)
        assert attempts == before, "a rejected call was retried against the dependency"

    async def test_only_declared_exceptions_are_retried(self) -> None:
        """Retrying a constraint violation never succeeds — it only multiplies load."""
        registry = BreakerRegistry(default_config=BreakerConfig(window_size=100, minimum_calls=100))
        guard = guarded(
            registry,
            "dep",
            policy=GuardPolicy(
                max_attempts=4, retry_on=(ConnectionError,), jitter=False, base_delay_seconds=0.001
            ),
        )
        attempts = 0

        async def bad_value() -> None:
            nonlocal attempts
            attempts += 1
            raise ValueError("malformed")

        with pytest.raises(ValueError):
            await guard.call(bad_value)
        assert attempts == 1

    async def test_a_transient_failure_is_absorbed(self) -> None:
        registry = BreakerRegistry(default_config=BreakerConfig(window_size=100, minimum_calls=100))
        guard = guarded(
            registry,
            "dep",
            policy=GuardPolicy(
                max_attempts=3, retry_on=(ConnectionError,), jitter=False, base_delay_seconds=0.001
            ),
        )
        attempts = 0

        async def flaky() -> str:
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise ConnectionError("blip")
            return "ok"

        assert await guard.call(flaky) == "ok"
        assert attempts == 3

    def test_jitter_spreads_retries(self) -> None:
        """Replicas retrying in lockstep arrive exactly when the dependency is recovering."""
        policy = GuardPolicy(base_delay_seconds=1.0, jitter=True)
        delays = {policy.delay_for(2) for _ in range(50)}
        assert len(delays) > 1, "jittered delays were identical"
        assert all(0.0 <= d <= 2.0 for d in delays)


# ---------------------------------------------------------------------------------------
# Outbox
# ---------------------------------------------------------------------------------------


class TestOutboxOrdering:
    async def test_only_the_head_of_a_partition_is_published(self) -> None:
        """The invariant. Without it a retried event is overtaken by its own successor."""
        store = InMemoryOutboxStore()
        tenant = uuid.uuid4()
        for n in range(3):
            await store.append(
                OutboxEvent(
                    event_type="patient.admitted",
                    tenant_id=tenant,
                    partition_key="patient-1",
                    payload={"n": n},
                )
            )
        broker = _Broker()
        publisher = OutboxPublisher(store, broker)

        assert await publisher.drain_once() == 1
        assert await publisher.drain_once() == 1
        assert await publisher.drain_once() == 1
        assert [v["payload"]["n"] for _, _, v in broker.sent] == [0, 1, 2]

    async def test_partitions_advance_independently(self) -> None:
        """Ordering is per partition; throughput comes from having many."""
        store = InMemoryOutboxStore()
        tenant = uuid.uuid4()
        for key in ("a", "b", "c"):
            await store.append(OutboxEvent(event_type="e", tenant_id=tenant, partition_key=key))
        broker = _Broker()

        assert await OutboxPublisher(store, broker).drain_once() == 3

    async def test_a_failed_event_blocks_its_own_partition_and_no_other(self) -> None:
        """The failure this design exists to prevent, checked directly.

        A relay that published "every due row" would deliver the second event of the blocked
        partition while the first sat in backoff — an admission processed after its discharge.
        """
        store = InMemoryOutboxStore()
        tenant = uuid.uuid4()
        blocked_first = await store.append(
            OutboxEvent(event_type="e", tenant_id=tenant, partition_key="blocked", payload={"i": 1})
        )
        await store.append(
            OutboxEvent(event_type="e", tenant_id=tenant, partition_key="blocked", payload={"i": 2})
        )
        await store.append(
            OutboxEvent(event_type="e", tenant_id=tenant, partition_key="free", payload={"i": 3})
        )

        broker = _Broker()
        broker.fail_times = 1  # the first claimed event fails
        publisher = OutboxPublisher(store, broker, config=PublisherConfig(base_retry_seconds=3600))
        await publisher.drain_once()

        # The free partition got through; the blocked one published nothing.
        assert [v["payload"]["i"] for _, _, v in broker.sent] == [3]

        await publisher.drain_once()
        assert [v["payload"]["i"] for _, _, v in broker.sent] == [3], (
            "an event overtook the failed head of its own partition"
        )
        assert store.get(blocked_first.event_id).attempts == 1


class TestOutboxDelivery:
    async def test_a_crash_before_marking_redelivers_rather_than_loses(self) -> None:
        """At-least-once. The row is marked published only after the broker acknowledges."""
        store = InMemoryOutboxStore()
        event = await store.append(_event())
        broker = _Broker()

        # Simulate a crash: claim, publish, and never record the outcome.
        async with store.claim(10) as batch:
            for claimed in batch.events:
                await broker.publish_message(
                    topic="t", key=claimed.partition_key, value=claimed.message()
                )
                # no batch.record_published — the process died here

        assert store.get(event.event_id).status is OutboxStatus.PENDING
        assert await OutboxPublisher(store, broker).drain_once() == 1
        assert len(broker.sent) == 2, "the event was lost rather than redelivered"
        assert broker.event_ids[0] == broker.event_ids[1], (
            "the redelivery carried a different id, so a consumer cannot deduplicate it"
        )

    async def test_an_unresolved_claim_does_not_consume_an_attempt(self) -> None:
        """A publisher crash must not march events toward the dead-letter queue."""
        store = InMemoryOutboxStore()
        event = await store.append(_event())

        async with store.claim(10):
            pass  # claimed, nothing recorded

        assert store.get(event.event_id).attempts == 0

    async def test_transient_failures_retry_then_dead_letter(self) -> None:
        store = InMemoryOutboxStore()
        event = await store.append(_event())
        broker = _Broker()
        broker.fail_times = 99
        publisher = OutboxPublisher(
            store, broker, config=PublisherConfig(max_attempts=3, base_retry_seconds=0.0)
        )

        for _ in range(3):
            await publisher.drain_once()

        stored = store.get(event.event_id)
        assert stored.status is OutboxStatus.DEAD
        assert publisher.stats.dead_lettered == 1

    async def test_a_dead_letter_reaches_the_dlq_topic(self) -> None:
        store = InMemoryOutboxStore()
        await store.append(_event())
        broker = _Broker()

        class OnlyDlqWorks(_Broker):
            async def publish_message(self, *, topic: str, key: str, value: dict) -> None:
                if not topic.endswith(".dlq"):
                    raise ConnectionError("broker unavailable")
                self.sent.append((topic, key, value))

        broker = OnlyDlqWorks()
        publisher = OutboxPublisher(
            store, broker, config=PublisherConfig(max_attempts=1, base_retry_seconds=0.0)
        )
        await publisher.drain_once()

        assert [t for t, _, _ in broker.sent] == ["cip.patient-admitted.dlq"]
        assert "deadLetterReason" in broker.sent[0][2]

    async def test_a_poison_message_dies_immediately(self) -> None:
        """A payload that can never serialise must not retry forever and block its partition."""
        store = InMemoryOutboxStore()
        event = await store.append(_event())

        class Poison(_Broker):
            async def publish_message(self, *, topic: str, key: str, value: dict) -> None:
                if topic.endswith(".dlq"):
                    self.sent.append((topic, key, value))
                    return
                raise TypeError("payload is not serialisable")

        broker = Poison()
        publisher = OutboxPublisher(store, broker, config=PublisherConfig(max_attempts=10))
        await publisher.drain_once()

        assert store.get(event.event_id).status is OutboxStatus.DEAD
        assert store.get(event.event_id).attempts == 1, "a permanent failure was retried"

    async def test_replay_returns_a_dead_event_and_resets_its_attempts(self) -> None:
        store = InMemoryOutboxStore()
        event = await store.append(_event())
        broker = _Broker()
        broker.fail_times = 99
        publisher = OutboxPublisher(
            store, broker, config=PublisherConfig(max_attempts=1, base_retry_seconds=0.0)
        )
        await publisher.drain_once()
        assert store.get(event.event_id).status is OutboxStatus.DEAD

        assert await store.replay(event.event_id) is True
        replayed = store.get(event.event_id)
        assert replayed.status is OutboxStatus.PENDING
        assert replayed.attempts == 0, (
            "a replayed event kept its exhausted attempts and would die on the first blip"
        )

        broker.fail_times = 0
        assert await publisher.drain_once() == 1

    async def test_a_duplicate_event_id_is_refused(self) -> None:
        """The id is the deduplication key; two rows under one id make it meaningless."""
        store = InMemoryOutboxStore()
        event = await store.append(_event())
        with pytest.raises(ValueError):
            await store.append(event)


class TestOutboxUnderFailure:
    async def test_an_open_circuit_does_not_consume_attempts(self) -> None:
        """During a broker outage every pending event would otherwise march to the DLQ.

        The events did nothing wrong; the broker is down. Charging them attempts would convert a
        recoverable outage into permanent data loss across the whole backlog.
        """
        store = InMemoryOutboxStore()
        tenant = uuid.uuid4()
        events = [
            await store.append(OutboxEvent(event_type="e", tenant_id=tenant, partition_key=f"p{i}"))
            for i in range(5)
        ]
        broker = _Broker()
        broker.fail_times = 999
        registry = BreakerRegistry(default_config=BreakerConfig(minimum_calls=2))
        publisher = OutboxPublisher(
            store,
            broker,
            config=PublisherConfig(max_attempts=3, base_retry_seconds=0.0),
            guard=guarded(registry, "kafka", policy=GuardPolicy(max_attempts=1)),
        )

        await publisher.drain_once()
        assert registry.get("kafka").state is BreakerState.OPEN
        assert publisher.stats.breaker_rejections >= 1

        # Every event rejected by the open circuit is still pending, not dead.
        for event in events:
            assert store.get(event.event_id).status is OutboxStatus.PENDING

    async def test_concurrent_publishers_never_publish_the_same_event(self) -> None:
        """Two relays racing. The claim must be exclusive or every event is delivered twice."""
        store = InMemoryOutboxStore()
        tenant = uuid.uuid4()
        for i in range(20):
            await store.append(OutboxEvent(event_type="e", tenant_id=tenant, partition_key=f"p{i}"))
        broker = _Broker()
        publishers = [
            OutboxPublisher(store, broker, config=PublisherConfig(batch_size=20)) for _ in range(4)
        ]

        await asyncio.gather(*(p.drain_once() for p in publishers))

        assert len(broker.event_ids) == len(set(broker.event_ids)), (
            "concurrent publishers delivered the same event more than once"
        )
        assert len(broker.event_ids) == 20

    async def test_the_relay_survives_an_unexpected_error(self) -> None:
        """A relay that dies stops every event on the platform, and the only symptom is a
        backlog nobody is watching yet."""
        store = InMemoryOutboxStore()
        await store.append(_event())

        class Exploding(_Broker):
            async def publish_message(self, **kwargs: object) -> None:
                raise RuntimeError("something unexpected")

        publisher = OutboxPublisher(
            store,
            Exploding(),
            config=PublisherConfig(
                poll_interval_seconds=0.01, max_attempts=2, base_retry_seconds=0.0
            ),
        )
        await publisher.start()
        await asyncio.sleep(0.1)
        assert publisher.is_running
        await publisher.stop()

    async def test_stats_expose_the_backlog_age(self) -> None:
        store = InMemoryOutboxStore()
        await store.append(_event())
        stats = await store.stats()
        assert stats.pending == 1
        assert stats.oldest_pending_age_seconds >= 0.0


class TestOutboxWithTheRealEventBus:
    async def test_the_relay_publishes_through_the_platform_bus(self) -> None:
        """The relay's producer contract is the one both buses implement."""
        store = InMemoryOutboxStore()
        await store.append(_event())
        bus = InMemoryEventBus()

        assert await OutboxPublisher(store, bus).drain_once() == 1
        assert len(bus.messages) == 1


class TestGuardErrorReporting:
    """Regression: the circuit opening mid-retry must not hide the cause.

    Found by `test_a_rejected_call_is_never_retried`. Attempt 1 failed with a real error, that
    failure tripped the breaker, and attempt 2 was rejected — so the caller received
    `BreakerOpenError` ("we did not try") instead of `ConnectionError` ("connection refused").
    A symptom replaced a diagnosis, and there was no way to recover the original.
    """

    async def test_the_original_error_survives_the_circuit_opening_mid_retry(self) -> None:
        registry = BreakerRegistry(default_config=BreakerConfig(minimum_calls=1))
        guard = guarded(
            registry,
            "dep",
            policy=GuardPolicy(
                max_attempts=5,
                retry_on=(ConnectionError,),
                jitter=False,
                base_delay_seconds=0.001,
            ),
        )

        async def boom() -> None:
            raise ConnectionError("connection refused")

        with pytest.raises(ConnectionError, match="connection refused"):
            await guard.call(boom)

    async def test_a_call_rejected_before_any_attempt_reports_the_open_circuit(self) -> None:
        """The other direction: nothing was tried, so the open circuit *is* the whole story."""
        registry = BreakerRegistry(default_config=BreakerConfig(minimum_calls=1))
        guard = guarded(registry, "dep", policy=GuardPolicy(max_attempts=1))

        async def boom() -> None:
            raise ConnectionError("down")

        with pytest.raises(ConnectionError):
            await guard.call(boom)
        with pytest.raises(BreakerOpenError):
            await guard.call(boom)


class TestPartitionOutageScenarios:
    """Named failure modes from the W7 brief, exercised end to end."""

    async def test_broker_downtime_then_recovery_loses_nothing(self) -> None:
        """Kafka down for a while, then back. Every event must arrive, in order, exactly once."""
        store = InMemoryOutboxStore()
        tenant = uuid.uuid4()
        for i in range(6):
            await store.append(
                OutboxEvent(
                    event_type="e", tenant_id=tenant, partition_key=f"p{i % 3}", payload={"i": i}
                )
            )
        broker = _Broker()
        broker.fail_times = 99  # the outage
        publisher = OutboxPublisher(
            store, broker, config=PublisherConfig(max_attempts=50, base_retry_seconds=0.0)
        )

        for _ in range(3):
            await publisher.drain_once()
        assert broker.sent == [], "something was published during the outage"

        broker.fail_times = 0  # recovery
        for _ in range(6):
            await publisher.drain_once()

        assert len(broker.event_ids) == 6
        assert len(set(broker.event_ids)) == 6, "recovery duplicated events"
        stats = await store.stats()
        assert stats.pending == 0 and stats.dead == 0
        # Per partition, arrival order must match creation order.
        for partition in ("p0", "p1", "p2"):
            arrived = [v["payload"]["i"] for _, k, v in broker.sent if k == partition]
            assert arrived == sorted(arrived), f"{partition} arrived out of order"

    async def test_a_retry_storm_is_bounded_by_the_breaker(self) -> None:
        """Without a breaker, N replicas x M retries all land on a struggling dependency."""
        store = InMemoryOutboxStore()
        tenant = uuid.uuid4()
        for i in range(30):
            await store.append(OutboxEvent(event_type="e", tenant_id=tenant, partition_key=f"p{i}"))

        calls = 0

        class Counting(_Broker):
            async def publish_message(self, **kwargs: object) -> None:
                nonlocal calls
                calls += 1
                raise ConnectionError("overloaded")

        registry = BreakerRegistry(default_config=BreakerConfig(minimum_calls=3, window_size=5))
        publisher = OutboxPublisher(
            store,
            Counting(),
            config=PublisherConfig(batch_size=30, max_attempts=50, base_retry_seconds=0.0),
            guard=guarded(registry, "kafka", policy=GuardPolicy(max_attempts=1)),
        )
        await publisher.drain_once()

        assert registry.get("kafka").state is BreakerState.OPEN
        assert calls < 30, (
            f"the breaker did not shed load: {calls} of 30 events still reached the broker"
        )

    async def test_slow_broker_is_treated_as_a_failure_not_a_hang(self) -> None:
        """A relay that waits forever on a hung broker stops delivering and never says why."""
        store = InMemoryOutboxStore()
        await store.append(_event())
        broker = _Broker()
        broker.hang_seconds = 5.0

        registry = BreakerRegistry(
            default_config=BreakerConfig(minimum_calls=1, call_timeout_seconds=0.05)
        )
        publisher = OutboxPublisher(
            store,
            broker,
            config=PublisherConfig(max_attempts=3, base_retry_seconds=0.0),
            guard=guarded(registry, "kafka", policy=GuardPolicy(max_attempts=1)),
        )

        await asyncio.wait_for(publisher.drain_once(), timeout=2.0)
        assert registry.get("kafka").state is BreakerState.OPEN
