#!/usr/bin/env python
"""Phase 4 benchmarks: the platform layer, and the subsystems it wraps.

Measures throughput and latency for the components a request actually passes through, plus
end-to-end figures for the Phase 2 and 3 pipelines so the platform's overhead is visible as a
fraction of real work rather than in isolation.

In-process, no network, deterministic model. These bound the platform's own cost — the whole
point of measuring them is to know that the cache lookup and rate-limit check on the request
path are negligible next to retrieval, which is the claim the architecture rests on.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import time
import tracemalloc
import uuid
from dataclasses import dataclass

TENANT = uuid.UUID("11111111-1111-4111-8111-111111111111")


@dataclass
class Result:
    name: str
    operations: int
    total_seconds: float
    unit: str = "op"

    @property
    def per_second(self) -> float:
        return self.operations / self.total_seconds if self.total_seconds else 0.0

    @property
    def micros_each(self) -> float:
        return (self.total_seconds / self.operations) * 1_000_000 if self.operations else 0.0

    def render(self) -> str:
        return (
            f"  {self.name:<44} {self.per_second:>12,.0f} {self.unit}/s "
            f"{self.micros_each:>10.2f} µs each"
        )


async def bench_cache(operations: int = 50_000) -> list[Result]:
    from cip_platform.cache.base import CacheDomain, CacheKey
    from cip_platform.cache.memory import InMemoryCache

    cache = InMemoryCache(max_entries=operations)
    keys = [CacheKey.for_content(CacheDomain.RETRIEVAL, TENANT, i) for i in range(operations)]

    started = time.perf_counter()
    for key in keys:
        await cache.set(key, {"v": 1}, ttl_seconds=300)
    write = time.perf_counter() - started

    started = time.perf_counter()
    for key in keys:
        await cache.get(key)
    read = time.perf_counter() - started

    started = time.perf_counter()
    await cache.invalidate_namespace(CacheDomain.RETRIEVAL, TENANT)
    sweep = time.perf_counter() - started

    return [
        Result("cache write", operations, write),
        Result("cache read (hit)", operations, read),
        Result("cache namespace sweep", operations, sweep, unit="key"),
    ]


def bench_limits(operations: int = 200_000) -> list[Result]:
    from cip_platform.security.limits import SpendBudget, TokenBucketLimiter

    limiter = TokenBucketLimiter(requests_per_minute=operations * 60, burst_multiplier=2.0)
    started = time.perf_counter()
    for _ in range(operations):
        limiter.check("tenant:x", scope="tenant")
    allow = time.perf_counter() - started

    # Worst case: every key is new, so eviction runs on every call.
    churn_limiter = TokenBucketLimiter(requests_per_minute=60, max_buckets=1000)
    churn_ops = 50_000
    started = time.perf_counter()
    for index in range(churn_ops):
        # A rejection is a valid outcome here; the measurement is the bookkeeping cost.
        with contextlib.suppress(Exception):
            churn_limiter.check(f"k{index}", scope="principal")
    churn = time.perf_counter() - started

    budget = SpendBudget(daily_limit_usd=1_000_000.0)
    started = time.perf_counter()
    for _ in range(operations):
        budget.charge(TENANT, 0.0001)
    charge = time.perf_counter() - started

    return [
        Result("rate limit check (warm)", operations, allow),
        Result("rate limit check (full churn)", churn_ops, churn),
        Result("budget charge", operations, charge),
    ]


def bench_auth(operations: int = 20_000) -> list[Result]:
    from cip_platform.security.identity import ApiKeyStore, Role, issue_api_key

    store = ApiKeyStore(pepper="bench")
    presented, record = issue_api_key(
        tenant_id=TENANT, roles=frozenset({Role.CLINICIAN}), pepper="bench"
    )
    store.add(record)

    started = time.perf_counter()
    for _ in range(operations):
        store.authenticate(presented)
    elapsed = time.perf_counter() - started
    return [Result("api key authentication", operations, elapsed)]


def bench_metrics(operations: int = 200_000) -> list[Result]:
    from cip_platform.observability.metrics import MetricRegistry

    registry = MetricRegistry()
    counter = registry.counter("bench_total", "", ("status",))
    histogram = registry.histogram("bench_seconds", "", ("route",))

    started = time.perf_counter()
    for _ in range(operations):
        counter.inc(status="ok")
    counter_time = time.perf_counter() - started

    started = time.perf_counter()
    for _ in range(operations):
        histogram.observe(0.05, route="/ask")
    histogram_time = time.perf_counter() - started

    started = time.perf_counter()
    for _ in range(1000):
        registry.render()
    render_time = time.perf_counter() - started

    return [
        Result("counter increment", operations, counter_time),
        Result("histogram observe", operations, histogram_time),
        Result("prometheus render", 1000, render_time, unit="scrape"),
    ]


async def bench_events(operations: int = 50_000) -> list[Result]:
    from cip_platform.events.base import Event, EventType
    from cip_platform.events.memory import InMemoryEventBus

    class _Noop:
        async def handle(self, event: Event) -> None:  # noqa: ARG002 - protocol signature
            return None

    bus = InMemoryEventBus()
    bus.subscribe(EventType.CHUNK_CREATED, _Noop())

    started = time.perf_counter()
    for index in range(operations):
        await bus.publish(
            Event(type=EventType.CHUNK_CREATED, tenant_id=TENANT, payload={"i": index})
        )
    elapsed = time.perf_counter() - started
    # Each publish also emits an audit record, so the bus does two dispatches per call.
    return [Result("event publish (incl. audit)", operations, elapsed)]


async def bench_pipelines() -> list[Result]:
    """End-to-end Phase 2 and Phase 3, so platform overhead has something to be a fraction of."""
    from cip_copilot.demo import _build_copilot, _graph
    from cip_copilot.domain import CopilotQuestion

    graph = await _graph()
    copilot = _build_copilot(graph)

    questions = [
        "What is her potassium trend?",
        "What are the active diagnoses?",
        "Do lisinopril and spironolactone interact?",
    ]
    latencies: list[float] = []
    for index in range(30):
        question = questions[index % len(questions)]
        started = time.perf_counter()
        await copilot.ask(
            CopilotQuestion(
                text=question,
                tenant_id=__import__("cip_copilot.demo", fromlist=["TENANT"]).TENANT,
                session_id=f"bench-{index}",
                patient_id=__import__("cip_copilot.demo", fromlist=["PATIENT"]).PATIENT,
            )
        )
        latencies.append(time.perf_counter() - started)

    total = sum(latencies)
    ordered = sorted(latencies)
    print(
        f"  {'copilot p50 / p95 / max':<44} "
        f"{ordered[len(ordered) // 2] * 1000:>7.1f} / "
        f"{ordered[int(len(ordered) * 0.95)] * 1000:.1f} / {ordered[-1] * 1000:.1f} ms"
    )
    return [Result("copilot end-to-end", len(latencies), total, unit="answer")]


async def main() -> int:
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8", errors="replace")

    tracemalloc.start()
    print("=" * 78)
    print("PHASE 4 BENCHMARKS — platform layer and wrapped subsystems")
    print("=" * 78)

    sections: list[tuple[str, list[Result]]] = []
    sections.append(("cache", await bench_cache()))
    sections.append(("limits", bench_limits()))
    sections.append(("security", bench_auth()))
    sections.append(("observability", bench_metrics()))
    sections.append(("events", await bench_events()))

    for name, results in sections:
        print(f"\n{name}")
        for result in results:
            print(result.render())

    print("\npipelines")
    pipeline_results = await bench_pipelines()
    for result in pipeline_results:
        print(result.render())

    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    print(f"\n  {'peak memory':<44} {peak / 1024 / 1024:>12.2f} MB")

    request_path = sum(
        r.micros_each
        for name, results in sections
        if name in ("cache", "limits", "security")
        for r in results
        if "warm" in r.name or r.name in ("cache read (hit)", "api key authentication")
    )
    copilot_micros = pipeline_results[0].micros_each
    print(
        f"\n  Platform overhead on the request path is ~{request_path:.0f} µs against a "
        f"~{copilot_micros / 1000:.1f} ms answer\n"
        f"  ({request_path / copilot_micros * 100:.2f}% of end-to-end). In-process figures: a "
        "real provider\n  and real datastores add network round-trips that dominate all of it."
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(asyncio.run(main()))
