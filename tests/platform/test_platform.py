"""Platform library: config, cache, events, tasks, observability, security, MLOps.

Most of what matters here is what the platform *refuses* — an unsafe deployment
configuration, an unscoped cache key, an unbounded metric label, a credential that does not
verify, an untested artifact combination.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from cip_platform.cache.base import CacheDomain, CacheKey, content_hash
from cip_platform.cache.domains import build_domains
from cip_platform.cache.memory import InMemoryCache
from cip_platform.config import (
    CachePolicy,
    Environment,
    LimitsPolicy,
    PlatformSettings,
    QueuePolicy,
    TelemetryPolicy,
    load_platform_settings,
)
from cip_platform.correlation import sanitise_correlation_id
from cip_platform.events.base import Event, EventType
from cip_platform.events.memory import InMemoryEventBus
from cip_platform.flags import FeatureFlag, FeatureFlags
from cip_platform.mlops.registry import (
    ArtifactKind,
    CompatibilityMatrix,
    DeploymentSet,
    EvaluationRecord,
    ModelRegistry,
    ModelVersion,
    RegistryError,
    Stage,
)
from cip_platform.observability.metrics import MetricRegistry, MetricsError
from cip_platform.observability.semconv import GEN_AI, LOCAL_EXTENSIONS
from cip_platform.security.identity import (
    ApiKeyStore,
    AuthenticationError,
    AuthorizationError,
    Role,
    Scope,
    issue_api_key,
    scopes_for_roles,
)
from cip_platform.security.limits import (
    BudgetDecision,
    RateLimitError,
    SpendBudget,
    TokenBucketLimiter,
)
from cip_platform.security.secrets import FileSecrets, SecretNotFoundError, StaticSecrets
from cip_platform.tasks.base import (
    JobKind,
    PermanentTaskError,
    TaskSpec,
    TaskStatus,
    TransientTaskError,
    backoff_seconds,
)
from cip_platform.tasks.memory import InMemoryTaskQueue

TENANT_A = uuid.UUID("11111111-1111-4111-8111-111111111111")
TENANT_B = uuid.UUID("22222222-2222-4222-8222-222222222222")


class TestConfiguration:
    def test_development_defaults_are_permissive(self) -> None:
        settings = PlatformSettings()
        assert settings.environment is Environment.DEVELOPMENT
        assert settings.cache.backend == "memory"

    @pytest.mark.parametrize("environment", [Environment.STAGING, Environment.PRODUCTION])
    def test_memory_backends_are_refused_when_deployed(self, environment: Environment) -> None:
        """Each of these fails silently in production, which is why it is refused loudly."""
        with pytest.raises(ValueError, match="per-replica"):
            PlatformSettings(environment=environment)

    def test_prompt_content_recording_is_refused_when_deployed(self) -> None:
        """Prompt text is PHI; attaching it to a span exports it to the telemetry backend."""
        with pytest.raises(ValueError, match="PHI"):
            PlatformSettings(
                environment=Environment.PRODUCTION,
                cache=CachePolicy(backend="redis", redis_url="redis://x"),
                queue=QueuePolicy(backend="redis", broker_url="redis://x:6379/1"),
                events_backend="kafka",
                telemetry=TelemetryPolicy(record_prompt_content=True),
            )

    def test_a_redis_backend_without_a_url_is_refused(self) -> None:
        with pytest.raises(ValueError, match="no redis_url"):
            PlatformSettings(
                environment=Environment.PRODUCTION,
                cache=CachePolicy(backend="redis"),
                queue=QueuePolicy(backend="redis", broker_url="redis://x:6379/1"),
                events_backend="kafka",
            )

    def test_a_valid_production_configuration_is_accepted(self) -> None:
        settings = PlatformSettings(
            environment=Environment.PRODUCTION,
            cache=CachePolicy(backend="redis", redis_url="redis://cache:6379/0"),
            queue=QueuePolicy(backend="redis", broker_url="redis://broker:6379/1"),
            events_backend="kafka",
            events_broker_url="kafka-0:9092,kafka-1:9092",
        )
        assert settings.environment.is_deployed

    def test_a_kafka_backend_without_a_broker_is_refused(self) -> None:
        """The same shape as the redis-without-a-url check, for the event backbone.

        Added in Phase 9 (W0): `events_backend` was validated as a name but nothing checked that
        a broker had been supplied, so a production deployment could name Kafka and start with
        nowhere to publish to.
        """
        with pytest.raises(ValueError, match="no events_broker_url"):
            PlatformSettings(
                environment=Environment.PRODUCTION,
                cache=CachePolicy(backend="redis", redis_url="redis://cache:6379/0"),
                queue=QueuePolicy(backend="redis", broker_url="redis://broker:6379/1"),
                events_backend="kafka",
            )

    def test_the_replaced_celery_backend_names_its_replacement(self) -> None:
        """An operator carrying the old value needs to be told what replaced it.

        Phase 9 replaced `celery` with `redis` (ADR-0040). Reporting it as merely "unknown"
        would leave a deployment stuck on a value that used to be correct.
        """
        with pytest.raises(ValueError, match="replaced by 'redis'"):
            QueuePolicy(backend="celery", broker_url="amqp://x")

    def test_the_principal_limit_is_tighter_than_the_tenant_limit(self) -> None:
        """One leaked key must not be able to consume its whole tenant's allowance."""
        limits = LimitsPolicy()
        assert limits.requests_per_minute_per_principal < limits.requests_per_minute_per_tenant

    def test_settings_load_from_a_supplied_mapping(self) -> None:
        settings = load_platform_settings(
            {"CIP_ENVIRONMENT": "testing", "CIP_RPM_PER_TENANT": "42"}
        )
        assert settings.environment is Environment.TESTING
        assert settings.limits.requests_per_minute_per_tenant == 42


class TestCacheKeys:
    def test_a_key_cannot_be_built_without_a_tenant(self) -> None:
        """A key collision produces a silent hit that looks exactly like a correct one."""
        with pytest.raises(TypeError):
            CacheKey(domain=CacheDomain.RETRIEVAL, discriminator="x")  # type: ignore[call-arg]

    def test_tenants_produce_different_keys_for_identical_content(self) -> None:
        left = CacheKey.for_content(CacheDomain.EMBEDDING, TENANT_A, "identical text")
        right = CacheKey.for_content(CacheDomain.EMBEDDING, TENANT_B, "identical text")
        assert left.render() != right.render()

    def test_the_tenant_precedes_the_discriminator(self) -> None:
        """So a namespace sweep is a prefix scan rather than a full keyspace walk."""
        key = CacheKey.for_content(CacheDomain.RETRIEVAL, TENANT_A, "q")
        assert key.render().startswith(CacheKey.namespace(CacheDomain.RETRIEVAL, TENANT_A))

    def test_content_hash_is_stable_and_order_sensitive(self) -> None:
        assert content_hash("a", "b") == content_hash("a", "b")
        assert content_hash("a", "b") != content_hash("b", "a")

    def test_an_empty_discriminator_is_refused(self) -> None:
        with pytest.raises(ValueError, match="discriminator"):
            CacheKey(domain=CacheDomain.PROMPT, tenant_id=TENANT_A, discriminator="  ")


class TestInMemoryCache:
    @pytest.fixture
    def clock(self) -> object:
        class Clock:
            now = 1000.0

            def __call__(self) -> float:
                return self.now

        return Clock()

    async def test_round_trips_a_value(self) -> None:
        cache = InMemoryCache()
        key = CacheKey.for_content(CacheDomain.RETRIEVAL, TENANT_A, "q")
        await cache.set(key, {"answer": 1}, ttl_seconds=60)
        assert await cache.get(key) == {"answer": 1}
        assert cache.stats().hits == 1

    async def test_an_expired_entry_is_a_miss(self, clock: object) -> None:
        cache = InMemoryCache(clock=clock)
        key = CacheKey.for_content(CacheDomain.SESSION, TENANT_A, "s")
        await cache.set(key, "v", ttl_seconds=10)
        clock.now += 11  # type: ignore[attr-defined]
        assert await cache.get(key) is None

    async def test_eviction_is_bounded(self) -> None:
        cache = InMemoryCache(max_entries=3)
        for index in range(5):
            await cache.set(
                CacheKey.for_content(CacheDomain.GRAPH, TENANT_A, index), index, ttl_seconds=60
            )
        assert cache.stats().evictions == 2

    async def test_namespace_invalidation_spares_other_tenants(self) -> None:
        cache = InMemoryCache()
        mine = CacheKey.for_content(CacheDomain.RETRIEVAL, TENANT_A, "q")
        theirs = CacheKey.for_content(CacheDomain.RETRIEVAL, TENANT_B, "q")
        await cache.set(mine, 1, ttl_seconds=60)
        await cache.set(theirs, 2, ttl_seconds=60)

        removed = await cache.invalidate_namespace(CacheDomain.RETRIEVAL, TENANT_A)
        assert removed == 1
        assert await cache.get(theirs) == 2

    async def test_namespace_invalidation_spares_other_domains(self) -> None:
        cache = InMemoryCache()
        retrieval = CacheKey.for_content(CacheDomain.RETRIEVAL, TENANT_A, "q")
        embedding = CacheKey.for_content(CacheDomain.EMBEDDING, TENANT_A, "q")
        await cache.set(retrieval, 1, ttl_seconds=60)
        await cache.set(embedding, 2, ttl_seconds=60)

        await cache.invalidate_namespace(CacheDomain.RETRIEVAL, TENANT_A)
        assert await cache.get(embedding) == 2

    async def test_a_write_only_invalidates_what_it_makes_stale(self) -> None:
        """Embeddings are content-addressed and prompts are versioned; sweeping them on an
        ingest would discard work for no correctness gain."""
        cache = InMemoryCache()
        domains = build_domains(cache, CachePolicy())
        for domain in CacheDomain:
            await cache.set(CacheKey.for_content(domain, TENANT_A, "x"), 1, ttl_seconds=60)

        swept = await domains.invalidate_tenant_writes(TENANT_A)
        assert set(swept) == {"retrieval", "graph"}
        assert await cache.get(CacheKey.for_content(CacheDomain.EMBEDDING, TENANT_A, "x")) == 1


class TestEvents:
    async def test_publishing_emits_an_audit_record(self) -> None:
        """Audit is a property of the bus, not of every developer remembering."""
        bus = InMemoryEventBus()
        await bus.publish(Event(type=EventType.CHUNK_CREATED, tenant_id=TENANT_A))
        assert len(bus.published(EventType.AUDIT_LOGGED)) == 1

    async def test_auditing_does_not_recurse(self) -> None:
        bus = InMemoryEventBus()
        await bus.publish(Event(type=EventType.AUDIT_LOGGED, tenant_id=TENANT_A))
        assert len(bus.published(EventType.AUDIT_LOGGED)) == 1

    async def test_a_phi_payload_is_summarised_to_its_keys(self) -> None:
        """The audit log is retained for years; clinical content in it is a second copy."""
        event = Event(
            type=EventType.CHUNK_CREATED,
            tenant_id=TENANT_A,
            payload={"text": "Potassium 5.4 mmol/L", "chunk_id": "c1"},
        )
        summary = event.audit_summary()
        assert summary["payload_keys"] == ["chunk_id", "text"]
        assert "5.4" not in str(summary)

    async def test_a_non_phi_payload_is_kept(self) -> None:
        event = Event(type=EventType.GRAPH_UPDATED, tenant_id=TENANT_A, payload={"nodes": 3})
        assert event.audit_summary()["payload_keys"] == {"nodes": 3}

    async def test_a_failing_handler_does_not_stop_the_others(self) -> None:
        bus = InMemoryEventBus()
        seen: list[str] = []

        class Broken:
            async def handle(self, event: Event) -> None:
                raise RuntimeError("boom")

        class Working:
            async def handle(self, event: Event) -> None:
                seen.append(str(event.type))

        bus.subscribe(EventType.CHUNK_CREATED, Broken())
        bus.subscribe(EventType.CHUNK_CREATED, Working())
        await bus.publish(Event(type=EventType.CHUNK_CREATED, tenant_id=TENANT_A))

        assert seen == ["ChunkCreated"]
        assert bus.failures()

    def test_a_caused_event_inherits_correlation_and_trace(self) -> None:
        """A handler that has to copy four fields will eventually copy three."""
        first = Event(
            type=EventType.DOCUMENT_UPLOADED,
            tenant_id=TENANT_A,
            correlation_id="corr-1",
            traceparent="00-abc-def-01",
        )
        second = first.caused(EventType.DOCUMENT_PARSED, document_id="d1")
        assert second.correlation_id == "corr-1"
        assert second.traceparent == "00-abc-def-01"
        assert second.causation_id == first.event_id

    def test_partitioning_is_per_tenant(self) -> None:
        event = Event(type=EventType.CHUNK_CREATED, tenant_id=TENANT_A)
        assert event.partition_key() == str(TENANT_A)


class TestTaskQueue:
    class _Ok:
        def __init__(self) -> None:
            self.calls = 0

        async def run(self, spec: TaskSpec) -> dict[str, int]:
            self.calls += 1
            return {"ran": self.calls}

    class _Flaky:
        def __init__(self, fail_times: int) -> None:
            self.calls = 0
            self._fail_times = fail_times

        async def run(self, spec: TaskSpec) -> dict[str, int]:
            self.calls += 1
            if self.calls <= self._fail_times:
                raise TransientTaskError("temporarily unavailable")
            return {"ran": self.calls}

    class _Broken:
        def __init__(self) -> None:
            self.calls = 0

        async def run(self, spec: TaskSpec) -> dict[str, int]:
            self.calls += 1
            raise PermanentTaskError("malformed payload")

    def _spec(self, **kwargs: object) -> TaskSpec:
        return TaskSpec(kind=JobKind.DOCUMENT_INGEST, tenant_id=TENANT_A, **kwargs)  # type: ignore[arg-type]

    async def test_a_successful_task_records_its_result(self) -> None:
        queue = InMemoryTaskQueue()
        handler = self._Ok()
        queue.register(JobKind.DOCUMENT_INGEST, handler)

        task_id = await queue.enqueue(self._spec())
        result = await queue.result(task_id)
        assert result is not None and result.succeeded

    async def test_a_transient_failure_is_retried(self) -> None:
        queue = InMemoryTaskQueue()
        handler = self._Flaky(fail_times=2)
        queue.register(JobKind.DOCUMENT_INGEST, handler)

        task_id = await queue.enqueue(self._spec(max_retries=3))
        result = await queue.result(task_id)
        assert result is not None and result.succeeded
        assert handler.calls == 3

    async def test_a_permanent_failure_is_not_retried(self) -> None:
        """Three attempts at a malformed payload is three identical failures."""
        queue = InMemoryTaskQueue()
        handler = self._Broken()
        queue.register(JobKind.DOCUMENT_INGEST, handler)

        task_id = await queue.enqueue(self._spec(max_retries=5))
        result = await queue.result(task_id)
        assert result is not None
        assert result.status is TaskStatus.DEAD_LETTERED
        assert handler.calls == 1

    async def test_exhausted_retries_are_dead_lettered_not_silent(self) -> None:
        queue = InMemoryTaskQueue()
        queue.register(JobKind.DOCUMENT_INGEST, self._Flaky(fail_times=99))

        await queue.enqueue(self._spec(max_retries=1))
        assert len(queue.dead_letters()) == 1

    async def test_an_idempotency_key_prevents_a_second_run(self) -> None:
        """At-least-once delivery means this will happen."""
        queue = InMemoryTaskQueue()
        handler = self._Ok()
        queue.register(JobKind.DOCUMENT_INGEST, handler)

        await queue.enqueue(self._spec(idempotency_key="doc-1"))
        await queue.enqueue(self._spec(idempotency_key="doc-1"))
        assert handler.calls == 1

    async def test_an_unroutable_task_is_dead_lettered_immediately(self) -> None:
        queue = InMemoryTaskQueue()
        await queue.enqueue(self._spec())
        assert len(queue.dead_letters()) == 1

    def test_job_kinds_route_to_separate_queues(self) -> None:
        """A long export must not sit in front of an ingest a clinician is waiting on."""
        assert JobKind.DOCUMENT_INGEST.queue != JobKind.EXPORT.queue

    def test_backoff_grows_and_is_capped(self) -> None:
        assert backoff_seconds(1) < backoff_seconds(3)
        assert backoff_seconds(50) == 300.0

    def test_backoff_rejects_a_zero_attempt(self) -> None:
        with pytest.raises(ValueError, match="attempt"):
            backoff_seconds(0)


class TestMetrics:
    def test_counter_and_histogram_render(self) -> None:
        registry = MetricRegistry()
        registry.counter("cip_test_total", "Test counter.", ("status",)).inc(status="ok")
        registry.histogram("cip_test_seconds", "Test histogram.", ("route",)).observe(
            0.2, route="/ask"
        )
        rendered = registry.render()
        assert 'cip_test_total{status="ok"} 1.0' in rendered
        assert "cip_test_seconds_bucket" in rendered
        assert "cip_test_seconds_count" in rendered

    def test_an_unbounded_label_is_refused(self) -> None:
        """A series per request is how a monitoring stack dies quietly."""
        registry = MetricRegistry(cardinality_limit=5)
        counter = registry.counter("cip_hot_total", "", ("request_id",))
        with pytest.raises(MetricsError, match="cardinality"):
            for index in range(10):
                counter.inc(request_id=f"req-{index}")

    def test_redeclaring_with_a_different_shape_is_refused(self) -> None:
        """Two incompatible series under one name silently mix in every query."""
        registry = MetricRegistry()
        registry.counter("cip_x_total", "", ("a",))
        with pytest.raises(MetricsError, match="already declared"):
            registry.counter("cip_x_total", "", ("b",))

    def test_wrong_labels_are_refused(self) -> None:
        registry = MetricRegistry()
        counter = registry.counter("cip_y_total", "", ("status",))
        with pytest.raises(MetricsError, match="expects labels"):
            counter.inc(other="x")

    def test_a_counter_cannot_decrease(self) -> None:
        registry = MetricRegistry()
        with pytest.raises(MetricsError, match="cannot decrease"):
            registry.counter("cip_z_total", "", ()).inc(-1)

    def test_nan_is_refused_by_a_histogram(self) -> None:
        """NaN poisons every quantile computed from the histogram."""
        registry = MetricRegistry()
        with pytest.raises(MetricsError):
            registry.histogram("cip_h_seconds", "", ()).observe(float("nan"))

    def test_label_values_are_escaped(self) -> None:
        registry = MetricRegistry()
        registry.counter("cip_esc_total", "", ("name",)).inc(name='has"quote')
        assert 'has\\"quote' in registry.render()


class TestSemanticConventions:
    def test_standard_attribute_names_are_used(self) -> None:
        """Telemetry is legible to any conformant backend without translation."""
        assert GEN_AI.USAGE_INPUT_TOKENS == "gen_ai.usage.input_tokens"
        assert GEN_AI.EVALUATION_SCORE_VALUE == "gen_ai.evaluation.score.value"
        assert GEN_AI.TOOL_NAME == "gen_ai.tool.name"

    def test_local_extensions_are_namespaced_and_enumerated(self) -> None:
        """So a reviewer can audit the non-standard surface in one place."""
        assert all(name.startswith("cip.") for name in LOCAL_EXTENSIONS)
        assert not any(name.startswith("gen_ai.") for name in LOCAL_EXTENSIONS)


class TestApiKeys:
    @pytest.fixture
    def store(self) -> ApiKeyStore:
        return ApiKeyStore(pepper="test-pepper")

    def test_a_minted_key_authenticates(self, store: ApiKeyStore) -> None:
        presented, record = issue_api_key(
            tenant_id=TENANT_A, roles=frozenset({Role.CLINICIAN}), pepper="test-pepper"
        )
        store.add(record)
        principal = store.authenticate(presented)
        assert principal.tenant_id == TENANT_A
        assert Scope.COPILOT_ASK in principal.scopes

    def test_the_secret_is_never_stored(self) -> None:
        presented, record = issue_api_key(
            tenant_id=TENANT_A, roles=frozenset({Role.CLINICIAN}), pepper="p"
        )
        assert presented.split("_")[-1] not in record.secret_hash

    def test_a_wrong_secret_is_refused(self, store: ApiKeyStore) -> None:
        _, record = issue_api_key(
            tenant_id=TENANT_A, roles=frozenset({Role.CLINICIAN}), pepper="test-pepper"
        )
        store.add(record)
        with pytest.raises(AuthenticationError):
            store.authenticate(f"cip_{record.prefix}_wrongsecret")

    def test_every_failure_gives_the_same_message(self, store: ApiKeyStore) -> None:
        """Distinguishing them tells an attacker which half of a guess was right."""
        messages = set()
        for candidate in ("garbage", "cip_deadbeef_nope", "cip__"):
            try:
                store.authenticate(candidate)
            except AuthenticationError as exc:
                messages.add(str(exc))
        assert messages == {"Invalid API key"}

    def test_a_revoked_key_stops_working(self, store: ApiKeyStore) -> None:
        presented, record = issue_api_key(
            tenant_id=TENANT_A, roles=frozenset({Role.CLINICIAN}), pepper="test-pepper"
        )
        store.add(record)
        assert store.revoke(record.key_id)
        with pytest.raises(AuthenticationError):
            store.authenticate(presented)

    def test_an_expired_key_is_refused(self, store: ApiKeyStore) -> None:
        presented, record = issue_api_key(
            tenant_id=TENANT_A,
            roles=frozenset({Role.CLINICIAN}),
            pepper="test-pepper",
            expires_at=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1),
        )
        store.add(record)
        with pytest.raises(AuthenticationError):
            store.authenticate(presented)

    def test_a_key_needs_at_least_one_role(self) -> None:
        with pytest.raises(ValueError, match="at least one role"):
            issue_api_key(tenant_id=TENANT_A, roles=frozenset(), pepper="p")


class TestRbac:
    def test_a_researcher_cannot_read_identified_patients(self) -> None:
        """A researcher who can read an identified record has defeated de-identification."""
        scopes = scopes_for_roles(frozenset({Role.RESEARCHER}))
        assert Scope.PATIENTS_READ not in scopes
        assert Scope.ANALYTICS_READ in scopes

    def test_only_a_tenant_admin_holds_admin(self) -> None:
        for role in Role:
            scopes = scopes_for_roles(frozenset({role}))
            assert (Scope.ADMIN in scopes) == (role is Role.TENANT_ADMIN)

    def test_roles_compose(self) -> None:
        combined = scopes_for_roles(frozenset({Role.READ_ONLY, Role.RESEARCHER}))
        assert Scope.ANALYTICS_READ in combined

    def test_require_raises_for_a_missing_scope(self) -> None:
        _, record = issue_api_key(tenant_id=TENANT_A, roles=frozenset({Role.READ_ONLY}), pepper="p")
        store = ApiKeyStore(pepper="p")
        store.add(record)
        principal = ApiKeyStore(pepper="p")  # noqa: F841 - constructed for clarity only
        from cip_platform.security.identity import Principal

        subject = Principal(
            principal_id="x",
            tenant_id=TENANT_A,
            roles=frozenset({Role.READ_ONLY}),
            scopes=scopes_for_roles(frozenset({Role.READ_ONLY})),
        )
        with pytest.raises(AuthorizationError):
            subject.require(Scope.ADMIN)

    def test_require_tenant_rejects_another_tenant(self) -> None:
        from cip_platform.security.identity import Principal

        subject = Principal(
            principal_id="x",
            tenant_id=TENANT_A,
            roles=frozenset({Role.CLINICIAN}),
            scopes=scopes_for_roles(frozenset({Role.CLINICIAN})),
        )
        with pytest.raises(AuthorizationError, match="different tenant"):
            subject.require_tenant(TENANT_B)


class TestRateLimiting:
    def test_a_burst_is_allowed_then_refused(self) -> None:
        limiter = TokenBucketLimiter(requests_per_minute=60, burst_multiplier=1.0)
        for _ in range(60):
            limiter.check("k", scope="tenant")
        with pytest.raises(RateLimitError) as excinfo:
            limiter.check("k", scope="tenant")
        assert excinfo.value.retry_after_seconds >= 1.0

    def test_the_bucket_refills(self) -> None:
        class Clock:
            now = 0.0

            def __call__(self) -> float:
                return self.now

        clock = Clock()
        limiter = TokenBucketLimiter(requests_per_minute=60, burst_multiplier=1.0, clock=clock)
        for _ in range(60):
            limiter.check("k", scope="tenant")
        clock.now = 30.0
        limiter.check("k", scope="tenant")

    def test_keys_are_independent(self) -> None:
        limiter = TokenBucketLimiter(requests_per_minute=1, burst_multiplier=1.0)
        limiter.check("a", scope="tenant")
        limiter.check("b", scope="tenant")

    def test_the_bucket_map_is_bounded(self) -> None:
        """An unbounded map is a memory leak driven by the traffic the limiter handles."""
        limiter = TokenBucketLimiter(requests_per_minute=60, max_buckets=10)
        for index in range(50):
            limiter.check(f"k{index}", scope="principal")
        assert limiter.tracked_keys() <= 10


class TestSpendBudget:
    def test_a_zero_limit_disables_the_control(self) -> None:
        budget = SpendBudget(daily_limit_usd=0.0)
        assert not budget.enabled
        assert budget.check(TENANT_A) is BudgetDecision.ALLOW

    def test_it_alerts_before_it_rejects(self) -> None:
        budget = SpendBudget(daily_limit_usd=10.0, alert_ratio=0.8)
        assert budget.charge(TENANT_A, 5.0) is BudgetDecision.ALLOW
        assert budget.charge(TENANT_A, 3.5) is BudgetDecision.ALERT
        assert budget.charge(TENANT_A, 2.0) is BudgetDecision.REJECT

    def test_tenants_have_separate_budgets(self) -> None:
        budget = SpendBudget(daily_limit_usd=1.0)
        budget.charge(TENANT_A, 5.0)
        assert budget.check(TENANT_B) is BudgetDecision.ALLOW

    def test_the_window_resets(self) -> None:
        days = [dt.date(2026, 3, 1)]
        budget = SpendBudget(daily_limit_usd=1.0, today=lambda: days[0])
        budget.charge(TENANT_A, 5.0)
        assert budget.check(TENANT_A) is BudgetDecision.REJECT
        days[0] = dt.date(2026, 3, 2)
        assert budget.check(TENANT_A) is BudgetDecision.ALLOW

    def test_a_negative_charge_is_refused(self) -> None:
        budget = SpendBudget(daily_limit_usd=1.0)
        with pytest.raises(ValueError, match="cost_usd"):
            budget.charge(TENANT_A, -1.0)

    def test_retry_after_points_at_the_window_boundary(self) -> None:
        """So a client retries when budget exists rather than into a wall."""
        budget = SpendBudget(daily_limit_usd=1.0)
        assert 0 < budget.seconds_until_reset() <= 86_400


class TestSecrets:
    def test_a_missing_secret_raises_without_leaking(self) -> None:
        with pytest.raises(SecretNotFoundError) as excinfo:
            StaticSecrets({}).require("db-password")
        assert "db-password" in str(excinfo.value)

    def test_file_secrets_read_from_a_directory(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        (tmp_path / "api-key-pepper").write_text("s3cr3t\n", encoding="utf-8")
        assert FileSecrets(tmp_path).require("api-key-pepper") == "s3cr3t"

    def test_traversal_is_refused(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """A secret name is chosen by this codebase; a separator means a bug or an attack."""
        with pytest.raises(SecretNotFoundError, match="bare name"):
            FileSecrets(tmp_path).get("../../etc/passwd")

    def test_file_secrets_are_reread_so_rotation_takes_effect(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        path = tmp_path / "token"
        path.write_text("old", encoding="utf-8")
        provider = FileSecrets(tmp_path)
        assert provider.get("token") == "old"
        path.write_text("new", encoding="utf-8")
        assert provider.get("token") == "new"


class TestFeatureFlags:
    def test_an_unknown_flag_is_off(self) -> None:
        """A removed flag should degrade to pre-feature behaviour, not a 500."""
        assert not FeatureFlags().is_on("nope")

    def test_a_rollout_is_stable_for_a_tenant(self) -> None:
        flags = FeatureFlags({"x": FeatureFlag(name="x", rollout_percent=50)})
        first = flags.is_on("x", tenant_id=TENANT_A)
        assert all(flags.is_on("x", tenant_id=TENANT_A) == first for _ in range(20))

    def test_a_rollout_splits_across_tenants(self) -> None:
        flag = FeatureFlag(name="x", rollout_percent=50)
        outcomes = {flag.is_on_for(uuid.uuid4()) for _ in range(60)}
        assert outcomes == {True, False}

    def test_a_rollout_without_a_tenant_is_off(self) -> None:
        """No tenant means no stable bucket; off keeps background paths out of a partial
        rollout rather than randomly inside it."""
        assert not FeatureFlag(name="x", rollout_percent=50).is_on_for(None)

    def test_an_invalid_percentage_is_refused(self) -> None:
        with pytest.raises(ValueError, match="rollout_percent"):
            FeatureFlag(name="x", rollout_percent=101)


class TestModelRegistry:
    def _version(self, version: str = "1.0.0") -> ModelVersion:
        return ModelVersion(
            name="clinical-embed",
            version=version,
            kind=ArtifactKind.EMBEDDING_MODEL,
            metadata={"dimensions": 768},
        )

    def test_promotion_demotes_the_incumbent(self) -> None:
        registry = ModelRegistry()
        registry.register(self._version("1.0.0"))
        registry.register(self._version("2.0.0"))
        registry.promote("clinical-embed", "1.0.0", to=Stage.PRODUCTION)
        registry.promote("clinical-embed", "2.0.0", to=Stage.PRODUCTION)

        assert registry.production("clinical-embed").version == "2.0.0"  # type: ignore[union-attr]
        assert registry.get("clinical-embed", "1.0.0").stage is Stage.STAGING

    def test_rollback_restores_the_previous_version(self) -> None:
        """One call, no deploy: the interval between noticing and reverting is seconds."""
        registry = ModelRegistry()
        registry.register(self._version("1.0.0"))
        registry.register(self._version("2.0.0"))
        registry.promote("clinical-embed", "1.0.0", to=Stage.PRODUCTION)
        registry.promote("clinical-embed", "2.0.0", to=Stage.PRODUCTION)

        rolled = registry.rollback("clinical-embed")
        assert rolled.version == "1.0.0"
        assert registry.get("clinical-embed", "2.0.0").stage is Stage.ARCHIVED

    def test_rollback_without_history_is_refused(self) -> None:
        registry = ModelRegistry()
        registry.register(self._version())
        with pytest.raises(RegistryError, match="No previous production"):
            registry.rollback("clinical-embed")

    def test_registering_straight_into_production_is_refused(self) -> None:
        """It skips the bookkeeping that makes rollback possible."""
        registry = ModelRegistry()
        with pytest.raises(RegistryError, match="cannot be registered directly"):
            registry.register(
                ModelVersion(
                    name="m", version="1", kind=ArtifactKind.LANGUAGE_MODEL, stage=Stage.PRODUCTION
                )
            )

    def test_duplicate_registration_is_refused(self) -> None:
        registry = ModelRegistry()
        registry.register(self._version())
        with pytest.raises(RegistryError, match="already registered"):
            registry.register(self._version())

    def test_embedding_dimensions_are_first_class(self) -> None:
        """A dimension change is the one change that makes an index unreadable."""
        assert self._version().dimensions == 768


class TestCompatibilityMatrix:
    def _record(self, **overrides: object) -> EvaluationRecord:
        payload = {
            "run_id": "r1",
            "model_version": "m1",
            "prompt_version": "p1",
            "embedding_version": "e1",
            "metrics": {"precision_at_1": 0.9, "hallucination_rate": 0.0},
        }
        payload.update(overrides)
        return EvaluationRecord(**payload)  # type: ignore[arg-type]

    def test_an_unevaluated_combination_is_refused(self) -> None:
        """Every component may be individually approved and the combination still untested."""
        matrix = CompatibilityMatrix()
        with pytest.raises(RegistryError, match="No evaluation exists"):
            matrix.require_supported(DeploymentSet("m1", "p1", "e1"))

    def test_an_evaluated_combination_is_accepted(self) -> None:
        matrix = CompatibilityMatrix(thresholds={"precision_at_1": 0.8})
        matrix.record(self._record())
        assert matrix.is_supported(DeploymentSet("m1", "p1", "e1"))

    def test_a_failing_combination_is_refused_with_the_shortfall(self) -> None:
        matrix = CompatibilityMatrix(thresholds={"precision_at_1": 0.95})
        matrix.record(self._record())
        with pytest.raises(RegistryError, match="did not meet thresholds"):
            matrix.require_supported(DeploymentSet("m1", "p1", "e1"))

    def test_a_missing_metric_fails_rather_than_passes(self) -> None:
        """Treating absence as success is how an evaluation gate stops gating."""
        matrix = CompatibilityMatrix(thresholds={"citation_accuracy": 0.9})
        matrix.record(self._record())
        assert not matrix.is_supported(DeploymentSet("m1", "p1", "e1"))

    def test_changing_one_component_invalidates_the_combination(self) -> None:
        matrix = CompatibilityMatrix(thresholds={"precision_at_1": 0.8})
        matrix.record(self._record())
        assert not matrix.is_supported(DeploymentSet("m1", "p1", "e2"))


class TestCorrelation:
    def test_a_valid_inbound_id_is_kept(self) -> None:
        assert sanitise_correlation_id("req-abc_123") == "req-abc_123"

    @pytest.mark.parametrize(
        "hostile",
        ["a" * 200, "has space", "new\nline", 'quote"', "semi;colon", ""],
    )
    def test_a_hostile_id_is_replaced(self, hostile: str) -> None:
        """The id lands in log lines and metric labels: injection and cardinality at once."""
        assert sanitise_correlation_id(hostile) != hostile

    def test_none_produces_an_id(self) -> None:
        assert len(sanitise_correlation_id(None)) == 32


class TestApiKeyFormatRegression:
    """Regression: `token_urlsafe` emits `_`, and an unbounded split shattered the secret.

    The failure was nondeterministic — it depended on the random bytes — so roughly one
    minted key in three was permanently unusable with no pattern to it.
    """

    def test_a_secret_containing_underscores_still_authenticates(self) -> None:
        from dataclasses import replace

        from cip_platform.security.identity import _hash_secret

        secret = "aa_bb_cc-dd_ee"
        _, record = issue_api_key(tenant_id=TENANT_A, roles=frozenset({Role.CLINICIAN}), pepper="p")
        record = replace(record, secret_hash=_hash_secret(secret, pepper="p"))

        store = ApiKeyStore(pepper="p")
        store.add(record)
        assert store.authenticate(f"cip_{record.prefix}_{secret}").tenant_id == TENANT_A

    def test_many_freshly_minted_keys_all_authenticate(self) -> None:
        """The original bug passed a single-key test roughly two times in three."""
        store = ApiKeyStore(pepper="p")
        presented_keys = []
        for _ in range(50):
            presented, record = issue_api_key(
                tenant_id=TENANT_A, roles=frozenset({Role.CLINICIAN}), pepper="p"
            )
            store.add(record)
            presented_keys.append(presented)

        for presented in presented_keys:
            assert store.authenticate(presented).tenant_id == TENANT_A

    @pytest.mark.parametrize("malformed", ["cip__secret", "cip_prefix_", "nope_a_b", "cip"])
    def test_malformed_keys_are_still_refused(self, malformed: str) -> None:
        with pytest.raises(AuthenticationError):
            ApiKeyStore(pepper="p").authenticate(malformed)


class TestResourceBounds:
    """Regressions for unbounded growth in long-lived components.

    All three of these are the *development* backends as well as the test ones, so they run
    in long-lived processes where "it's only a test helper" is not true.
    """

    def test_limiter_eviction_does_not_degrade_with_churn(self) -> None:
        """Regression: eviction scanned every bucket, so once the map was full an attacker
        rotating principal identifiers forced an O(n) scan per request — turning the control
        that prevents resource exhaustion into a way to cause it."""
        import time as _time

        limiter = TokenBucketLimiter(requests_per_minute=60, max_buckets=2000)

        started = _time.perf_counter()
        for index in range(2000):
            limiter.check(f"warm-{index}", scope="principal")
        fill = _time.perf_counter() - started

        started = _time.perf_counter()
        for index in range(2000):
            limiter.check(f"churn-{index}", scope="principal")
        churn = _time.perf_counter() - started

        assert limiter.tracked_keys() <= 2000
        # With an O(n) scan the churn phase was orders of magnitude slower than the fill.
        # A generous factor keeps this from being flaky while still failing the old code.
        assert churn < fill * 10 + 0.5

    async def test_event_history_is_bounded(self) -> None:
        """The bus retained full payloads forever — a leak and a PHI-retention problem."""
        bus = InMemoryEventBus(history_limit=50)
        for index in range(500):
            await bus.publish(
                Event(
                    type=EventType.CHUNK_CREATED,
                    tenant_id=TENANT_A,
                    payload={"text": f"clinical content {index}"},
                )
            )
        assert len(bus.published()) <= 50

    async def test_task_queue_state_is_bounded(self) -> None:
        class _Ok:
            async def run(self, spec: TaskSpec) -> dict[str, int]:
                return {}

        queue = InMemoryTaskQueue(history_limit=25)
        queue.register(JobKind.MAINTENANCE, _Ok())
        for index in range(300):
            await queue.enqueue(
                TaskSpec(
                    kind=JobKind.MAINTENANCE,
                    tenant_id=TENANT_A,
                    idempotency_key=f"key-{index}",
                )
            )
        assert len(queue.dead_letters()) <= 25

    async def test_idempotency_still_holds_within_the_window(self) -> None:
        """Bounding the dedupe set must not break deduplication for recent tasks."""

        class _Counting:
            def __init__(self) -> None:
                self.calls = 0

            async def run(self, spec: TaskSpec) -> dict[str, int]:
                self.calls += 1
                return {}

        handler = _Counting()
        queue = InMemoryTaskQueue(history_limit=100)
        queue.register(JobKind.MAINTENANCE, handler)

        for _ in range(5):
            await queue.enqueue(
                TaskSpec(kind=JobKind.MAINTENANCE, tenant_id=TENANT_A, idempotency_key="same")
            )
        assert handler.calls == 1


class TestManifestPolicy:
    """The deployment policy checks, which a schema validator cannot express."""

    def test_the_shipped_manifests_pass(self) -> None:
        import sys

        sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2]))
        from scripts.validate_manifests import validate_directory

        violations = validate_directory()
        assert not violations, "\n".join(v.render() for v in violations)

    def test_a_root_container_is_caught(self) -> None:
        import sys

        sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2]))
        from scripts.validate_manifests import validate_document

        bad = {
            "kind": "Deployment",
            "metadata": {"name": "bad"},
            "spec": {
                "template": {
                    "spec": {
                        "containers": [{"name": "c", "image": "img:1.0", "securityContext": {}}]
                    }
                }
            },
        }
        rules = {v.rule for v in validate_document(bad, path="x")}
        assert "run-as-non-root" in rules
        assert "drop-capabilities" in rules
        assert "memory-limit" in rules

    def test_a_floating_image_tag_is_caught(self) -> None:
        """The image scanned and the image running can otherwise differ silently."""
        import sys

        sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2]))
        from scripts.validate_manifests import validate_document

        bad = {
            "kind": "Deployment",
            "metadata": {"name": "bad"},
            "spec": {"template": {"spec": {"containers": [{"name": "c", "image": "img:latest"}]}}},
        }
        assert "image-tag" in {v.rule for v in validate_document(bad, path="x")}
