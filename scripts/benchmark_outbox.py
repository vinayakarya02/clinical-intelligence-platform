"""W7 benchmarks: the breaker and the relay.

**These assert.** The gap analysis found that every existing benchmark printed numbers and
returned 0 unconditionally, so a performance regression was invisible unless somebody read the
output. Each figure below carries a threshold, set generously against measured values so the
check fails on a real regression rather than on a busy machine.

Run with ``python scripts/benchmark_outbox.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import time
import uuid

from cip_platform.outbox import InMemoryOutboxStore, OutboxEvent, OutboxPublisher, PublisherConfig
from cip_platform.resilience import BreakerConfig, BreakerRegistry, CircuitBreaker


class _Sink:
    def __init__(self) -> None:
        self.count = 0

    async def publish_message(self, **kwargs: object) -> None:
        del kwargs  # the sink measures throughput; the message content is irrelevant here
        self.count += 1


async def _bench_breaker_overhead(iterations: int = 20_000) -> float:
    """Cost of the breaker on the happy path. It wraps every external call, so this is paid
    on every query the platform makes — it has to be negligible."""
    breaker = CircuitBreaker("bench", BreakerConfig(window_size=100, minimum_calls=100))

    async def noop() -> int:
        return 1

    began = time.perf_counter()
    for _ in range(iterations):
        await breaker.call(noop)
    return (time.perf_counter() - began) / iterations * 1_000_000  # microseconds


async def _bench_rejection(iterations: int = 20_000) -> float:
    """Cost of a rejected call. During an outage this is the hot path, and it must be far
    cheaper than the call it replaces or the breaker adds load instead of shedding it."""
    registry = BreakerRegistry(default_config=BreakerConfig(minimum_calls=1))
    breaker = registry.get("bench")
    await breaker.trip("benchmark")

    async def noop() -> int:
        return 1

    began = time.perf_counter()
    for _ in range(iterations):
        with contextlib.suppress(Exception):
            await breaker.call(noop)
    return (time.perf_counter() - began) / iterations * 1_000_000


async def _bench_relay(events: int = 5_000) -> tuple[float, float]:
    """End-to-end relay throughput, one partition per event (the parallel case)."""
    store = InMemoryOutboxStore()
    tenant = uuid.uuid4()
    began = time.perf_counter()
    for i in range(events):
        await store.append(
            OutboxEvent(
                event_type="bench.event",
                tenant_id=tenant,
                partition_key=f"p{i}",
                payload={"i": i},
            )
        )
    append_rate = events / (time.perf_counter() - began)

    sink = _Sink()
    publisher = OutboxPublisher(store, sink, config=PublisherConfig(batch_size=500))
    began = time.perf_counter()
    while await publisher.drain_once():
        pass
    publish_rate = sink.count / (time.perf_counter() - began)
    assert sink.count == events, f"relay published {sink.count} of {events}"
    return append_rate, publish_rate


async def main() -> int:
    print("W7 benchmarks\n" + "=" * 60)
    failures: list[str] = []

    overhead = await _bench_breaker_overhead()
    rejection = await _bench_rejection()
    ratio = overhead / rejection if rejection else float("inf")
    print(f"  breaker overhead (closed)   {overhead:8.2f} us/call")
    print(f"  breaker rejection (open)    {rejection:8.2f} us/call")
    print(f"  closed:open ratio           {ratio:8.1f} x")

    # An absolute ceiling, set with headroom. Most of the closed-path cost is the timeout's
    # timer handle, and timer granularity is platform-dependent — it is materially cheaper on
    # the Linux hosts that run CI and production than on a Windows developer machine. A tight
    # absolute bound would fail for the wrong reason.
    if overhead > 150.0:
        failures.append(f"breaker overhead {overhead:.2f}us exceeds 150us")
    if rejection > 25.0:
        failures.append(f"rejection cost {rejection:.2f}us exceeds 25us")

    # The check that actually catches a regression. `asyncio.wait_for` wraps every call in a
    # Task and measured a 30x ratio; `asyncio.timeout` measures around 15x. A ratio bound
    # catches that reintroduction on any machine, where an absolute bound tuned on one host
    # would not.
    if ratio > 22.0:
        failures.append(
            f"closed path is {ratio:.1f}x the open path (limit 22x) — the breaker is spending "
            "more on protection than on shedding load; check the per-call timeout mechanism"
        )

    append_rate, publish_rate = await _bench_relay()
    print(f"  outbox append               {append_rate:8.0f} events/s")
    print(f"  relay publish               {publish_rate:8.0f} events/s")
    if append_rate < 20_000:
        failures.append(f"append rate {append_rate:.0f}/s below 20,000/s")
    if publish_rate < 8_000:
        failures.append(f"publish rate {publish_rate:.0f}/s below 8,000/s")

    print("=" * 60)
    if failures:
        print("FAILED")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("all thresholds met")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
