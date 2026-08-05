"""Environment-aware platform configuration.

Complements ``cip_core.config`` rather than replacing it: that owns the application's stores
and credentials, this owns the production surface added in Phase 4 — caching, queues, events,
telemetry, limits, and budgets.

The design rule is that **defaults differ per environment and unsafe combinations are refused
at construction.** A configuration error found at startup is an outage of seconds; the same
error found because a cache was silently disabled in production is a latency incident nobody
can explain. Every check below exists because the failure it prevents is invisible.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from enum import StrEnum

__all__ = [
    "CachePolicy",
    "Environment",
    "PlatformSettings",
    "QueuePolicy",
    "TelemetryPolicy",
    "load_platform_settings",
]


class Environment(StrEnum):
    """Where this process is running.

    ``TESTING`` is distinct from ``DEVELOPMENT`` because tests need determinism — no
    background flushes, no sampling, no retries with jitter — while development wants the
    production code paths exercised.
    """

    DEVELOPMENT = "development"
    TESTING = "testing"
    STAGING = "staging"
    PRODUCTION = "production"

    @classmethod
    def _missing_(cls, value: object) -> Environment | None:
        """Accept ``cip_core``'s short spellings — see that enum for why both exist."""
        if not isinstance(value, str):
            return None
        return {
            "prod": cls.PRODUCTION,
            "dev": cls.DEVELOPMENT,
            "local": cls.DEVELOPMENT,
            "test": cls.TESTING,
        }.get(value.strip().lower())

    @property
    def is_deployed(self) -> bool:
        """Whether this environment serves anything a user could depend on."""
        return self in (Environment.STAGING, Environment.PRODUCTION)


@dataclass(frozen=True, slots=True)
class CachePolicy:
    """Cache backend and per-domain lifetimes."""

    backend: str = "memory"
    """``memory`` or ``redis``. Refused as ``memory`` in a deployed environment: a per-replica
    cache in a multi-replica deployment produces a hit rate that falls as you scale out, which
    presents as a capacity problem rather than as the configuration mistake it is."""

    redis_url: str = ""
    max_entries: int = 10_000
    embedding_ttl_seconds: int = 30 * 24 * 3600
    retrieval_ttl_seconds: int = 15 * 60
    session_ttl_seconds: int = 2 * 3600
    prompt_ttl_seconds: int = 3600
    graph_ttl_seconds: int = 3600

    def __post_init__(self) -> None:
        if self.backend not in ("memory", "redis"):
            raise ValueError(f"Unknown cache backend '{self.backend}'")
        for name in (
            "embedding_ttl_seconds",
            "retrieval_ttl_seconds",
            "session_ttl_seconds",
            "prompt_ttl_seconds",
            "graph_ttl_seconds",
        ):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be >= 1")


@dataclass(frozen=True, slots=True)
class QueuePolicy:
    """Background work backend and retry behaviour."""

    backend: str = "memory"
    """``memory`` or ``celery``. Memory executes inline, which is right for tests and wrong for
    anything that must survive a restart."""

    broker_url: str = ""
    max_retries: int = 3
    retry_backoff_seconds: float = 2.0
    visibility_timeout_seconds: int = 900
    """How long a claimed job may run before the broker redelivers it. Must exceed the slowest
    job or the queue will redeliver work that is still running, and two workers will ingest the
    same document."""

    def __post_init__(self) -> None:
        if self.backend not in ("memory", "celery"):
            raise ValueError(f"Unknown queue backend '{self.backend}'")
        if self.max_retries < 0:
            raise ValueError("max_retries must be >= 0")


@dataclass(frozen=True, slots=True)
class TelemetryPolicy:
    """Metrics, tracing, and what may be recorded."""

    service_name: str = "clinical-intelligence-platform"
    metrics_enabled: bool = True
    tracing_enabled: bool = True
    trace_sample_ratio: float = 1.0
    otlp_endpoint: str = ""

    record_prompt_content: bool = False
    """Whether prompt and completion text may be attached to spans. The GenAI conventions
    support it and it is invaluable for debugging — and for this platform that text is PHI, so
    it is refused outright in deployed environments rather than left to a reviewer to notice."""

    def __post_init__(self) -> None:
        if not 0.0 <= self.trace_sample_ratio <= 1.0:
            raise ValueError("trace_sample_ratio must be in [0, 1]")


@dataclass(frozen=True, slots=True)
class LimitsPolicy:
    """Rate limits and spend budgets."""

    requests_per_minute_per_tenant: int = 600
    requests_per_minute_per_principal: int = 120
    """Lower than the tenant limit on purpose: one leaked API key must not be able to consume
    its tenant's entire allowance."""

    burst_multiplier: float = 2.0
    daily_budget_usd_per_tenant: float = 0.0
    """Zero disables the budget. Non-zero enables ALERT at ``budget_alert_ratio`` and REJECT at
    the limit (ADR-0018)."""

    budget_alert_ratio: float = 0.8
    max_request_bytes: int = 8 * 1024 * 1024

    def __post_init__(self) -> None:
        if self.requests_per_minute_per_tenant < 1:
            raise ValueError("requests_per_minute_per_tenant must be >= 1")
        if not 0.0 < self.budget_alert_ratio < 1.0:
            raise ValueError("budget_alert_ratio must be strictly between 0 and 1")
        if self.daily_budget_usd_per_tenant < 0:
            raise ValueError("daily_budget_usd_per_tenant must be >= 0")


@dataclass(frozen=True, slots=True)
class PlatformSettings:
    """The Phase 4 configuration surface."""

    environment: Environment = Environment.DEVELOPMENT
    cache: CachePolicy = field(default_factory=CachePolicy)
    queue: QueuePolicy = field(default_factory=QueuePolicy)
    telemetry: TelemetryPolicy = field(default_factory=TelemetryPolicy)
    limits: LimitsPolicy = field(default_factory=LimitsPolicy)
    events_backend: str = "memory"
    feature_flags: dict[str, bool] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.events_backend not in ("memory", "kafka"):
            raise ValueError(f"Unknown events backend '{self.events_backend}'")
        if self.environment.is_deployed:
            self._refuse_unsafe_deployment()

    def _refuse_unsafe_deployment(self) -> None:
        """Reject configurations that are safe locally and wrong in a deployed environment.

        Each of these fails *silently* in production — no error, just degraded behaviour that
        looks like something else — which is exactly the class of mistake worth refusing at
        startup.
        """
        problems: list[str] = []

        if self.cache.backend == "memory":
            problems.append(
                "cache backend 'memory' is per-replica; hit rate falls as replicas scale, "
                "which presents as a capacity problem rather than a config error"
            )
        if self.queue.backend == "memory":
            problems.append(
                "queue backend 'memory' executes inline and loses queued work on restart"
            )
        if self.events_backend == "memory":
            problems.append("events backend 'memory' does not survive a process restart")
        if self.telemetry.record_prompt_content:
            problems.append(
                "record_prompt_content attaches PHI to spans, which exports it to the "
                "telemetry backend"
            )
        if not self.telemetry.metrics_enabled:
            problems.append("metrics disabled: the service would be unobservable")
        if self.cache.backend == "redis" and not self.cache.redis_url:
            problems.append("cache backend is 'redis' but no redis_url is set")
        if self.queue.backend == "celery" and not self.queue.broker_url:
            problems.append("queue backend is 'celery' but no broker_url is set")

        if problems:
            raise ValueError(
                f"Unsafe configuration for {self.environment}:\n  - " + "\n  - ".join(problems)
            )

    def for_environment(self, environment: Environment) -> PlatformSettings:
        """This configuration with production-appropriate defaults for ``environment``."""
        if environment is Environment.TESTING:
            return replace(
                self,
                environment=environment,
                telemetry=replace(self.telemetry, trace_sample_ratio=1.0, tracing_enabled=False),
                queue=replace(self.queue, backend="memory", max_retries=0),
            )
        return replace(self, environment=environment)


def load_platform_settings(source: dict[str, str] | None = None) -> PlatformSettings:
    """Build settings from the environment.

    Reads a mapping rather than ``os.environ`` directly so a test can supply one without
    mutating global state — the same reason ``cip_core`` takes an explicit source.
    """
    env = source if source is not None else dict(os.environ)

    def _get(name: str, default: str = "") -> str:
        return env.get(f"CIP_{name}", default).strip()

    def _int(name: str, default: int) -> int:
        raw = _get(name)
        return int(raw) if raw else default

    def _float(name: str, default: float) -> float:
        raw = _get(name)
        return float(raw) if raw else default

    def _bool(name: str, default: bool) -> bool:
        raw = _get(name).lower()
        return raw in ("1", "true", "yes") if raw else default

    environment = Environment(_get("ENVIRONMENT", "development") or "development")

    return PlatformSettings(
        environment=environment,
        cache=CachePolicy(
            backend=_get("CACHE_BACKEND", "memory") or "memory",
            redis_url=_get("REDIS_URL"),
            max_entries=_int("CACHE_MAX_ENTRIES", 10_000),
        ),
        queue=QueuePolicy(
            backend=_get("QUEUE_BACKEND", "memory") or "memory",
            broker_url=_get("BROKER_URL"),
            max_retries=_int("QUEUE_MAX_RETRIES", 3),
        ),
        telemetry=TelemetryPolicy(
            service_name=_get("SERVICE_NAME", "clinical-intelligence-platform")
            or "clinical-intelligence-platform",
            metrics_enabled=_bool("METRICS_ENABLED", True),
            tracing_enabled=_bool("TRACING_ENABLED", True),
            trace_sample_ratio=_float("TRACE_SAMPLE_RATIO", 1.0),
            otlp_endpoint=_get("OTLP_ENDPOINT"),
            record_prompt_content=_bool("RECORD_PROMPT_CONTENT", False),
        ),
        limits=LimitsPolicy(
            requests_per_minute_per_tenant=_int("RPM_PER_TENANT", 600),
            requests_per_minute_per_principal=_int("RPM_PER_PRINCIPAL", 120),
            daily_budget_usd_per_tenant=_float("DAILY_BUDGET_USD", 0.0),
        ),
        events_backend=_get("EVENTS_BACKEND", "memory") or "memory",
    )
