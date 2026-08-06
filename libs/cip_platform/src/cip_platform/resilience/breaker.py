"""Circuit breakers.

Retry without a breaker is a retry storm. Every remote dependency in this platform already has
retries and timeouts; W1 adds four more remote dependencies, and the arithmetic is unforgiving —
three replicas, each retrying three times against a database that has become slow rather than
down, is nine times the load on the thing that is already struggling. The breaker is what turns
that into a fast local failure.

**Built rather than adopted, deliberately.** ``pybreaker`` is thread-oriented and its state is
guarded by a lock designed for synchronous callers; ``aiobreaker`` is a thin fork with little
activity. Neither implements a sliding failure window or a half-open success threshold, which
are the two properties that make a breaker useful rather than merely present:

- **Fixed counters flap.** A breaker that counts consecutive failures reopens on one bad request
  after a recovery, and a breaker that counts total failures never opens on a dependency that
  fails one call in three. A sliding window over recent outcomes measures the thing that matters
  — the current failure *rate*.
- **A single probe proves nothing.** Closing after one successful half-open call reopens the
  circuit onto a dependency that answered once by luck. Requiring several consecutive successes
  is what distinguishes recovery from noise.

Three states, and the transitions are the whole design:

```
         failure rate >= threshold
CLOSED ───────────────────────────▶ OPEN
   ▲                                  │ reset timeout elapsed
   │  success_threshold consecutive   ▼
   └───────────── HALF_OPEN ◀─────────┘
                     │
                     └─ any failure ─▶ OPEN  (immediately, without waiting for the window)
```

**Half-open admits a bounded number of concurrent probes**, not one and not unlimited. One
serialises recovery behind a single request; unlimited sends the full load back at a dependency
that has just come up, which is how a recovering database is knocked over a second time.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, TypeVar

from cip_core.errors import CipError
from cip_core.logging import get_logger

__all__ = [
    "BreakerConfig",
    "BreakerOpenError",
    "BreakerRegistry",
    "BreakerState",
    "BreakerStats",
    "CircuitBreaker",
]

_log = get_logger(__name__)

T = TypeVar("T")


class BreakerOpenError(CipError):
    """The circuit is open and the call was not attempted.

    A distinct type, not a generic error: "we did not try" and "we tried and it failed" are
    different facts. A caller can reasonably serve stale data for the first and must not for the
    second, and a metric that conflates them cannot show that a breaker is doing its job.
    """

    status = 503
    problem_type = "circuit-open"
    title = "Dependency unavailable"

    def __init__(self, name: str, *, retry_after_seconds: float) -> None:
        self.dependency = name
        self.retry_after_seconds = retry_after_seconds
        super().__init__(
            f"circuit for {name!r} is open; not attempting for another {retry_after_seconds:.1f}s"
        )


class BreakerState(StrEnum):
    CLOSED = "closed"
    """Calls pass through. Outcomes are recorded in the sliding window."""
    OPEN = "open"
    """Calls fail immediately. No load reaches the dependency."""
    HALF_OPEN = "half_open"
    """A bounded number of probes are admitted to test recovery."""

    @property
    def admits_traffic(self) -> bool:
        return self is not BreakerState.OPEN


@dataclass(frozen=True, slots=True)
class BreakerConfig:
    """How one breaker behaves.

    Per dependency, because a Kafka publish and a Postgres query have different latencies,
    different failure modes, and different acceptable recovery times. One shared configuration
    would be tuned for whichever dependency was noticed first.
    """

    failure_threshold: float = 0.5
    """Failure *rate* over the window at which the circuit opens, in (0, 1]."""

    window_size: int = 20
    """Outcomes retained. Small enough to react within a few seconds of a real outage; large
    enough that two unlucky failures in a quiet period do not open the circuit."""

    minimum_calls: int = 5
    """Outcomes required before the rate is trusted. Without this, the first failed call is a
    100% failure rate and every breaker opens on its first hiccup."""

    reset_timeout_seconds: float = 30.0
    """How long OPEN lasts before a probe is admitted."""

    success_threshold: int = 3
    """Consecutive half-open successes required to close."""

    half_open_max_calls: int = 3
    """Concurrent probes admitted while half-open."""

    call_timeout_seconds: float = 10.0
    """Per-call ceiling. A call that hangs is a failure that never gets counted, so a breaker
    without its own timeout cannot open on the failure mode that hurts most — a dependency that
    accepts connections and never answers."""

    def __post_init__(self) -> None:
        if not 0.0 < self.failure_threshold <= 1.0:
            raise ValueError("failure_threshold must be in (0, 1]")
        if self.window_size < 1:
            raise ValueError("window_size must be >= 1")
        if self.minimum_calls < 1:
            raise ValueError("minimum_calls must be >= 1")
        if self.minimum_calls > self.window_size:
            raise ValueError("minimum_calls cannot exceed window_size")
        if self.reset_timeout_seconds <= 0:
            raise ValueError("reset_timeout_seconds must be > 0")
        if self.success_threshold < 1:
            raise ValueError("success_threshold must be >= 1")
        if self.half_open_max_calls < 1:
            raise ValueError("half_open_max_calls must be >= 1")
        if self.call_timeout_seconds <= 0:
            raise ValueError("call_timeout_seconds must be > 0")


@dataclass(slots=True)
class BreakerStats:
    """What one breaker has seen. Reported to health and metrics."""

    name: str
    state: BreakerState = BreakerState.CLOSED
    calls: int = 0
    failures: int = 0
    successes: int = 0
    rejected: int = 0
    """Calls not attempted because the circuit was open. The number an operator actually wants:
    it is the load the breaker shed."""
    timeouts: int = 0
    opened_count: int = 0
    last_error: str = ""
    last_state_change: float = 0.0
    consecutive_successes: int = 0

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "state": str(self.state),
            "calls": self.calls,
            "failures": self.failures,
            "successes": self.successes,
            "rejected": self.rejected,
            "timeouts": self.timeouts,
            "openedCount": self.opened_count,
            "lastError": self.last_error,
        }


class CircuitBreaker:
    """One breaker, guarding one dependency.

    Not reentrant across dependencies by design: :class:`BreakerRegistry` holds one instance per
    dependency so a slow Neo4j cannot open the circuit in front of Postgres. Sharing a breaker
    between dependencies is the mistake that makes a breaker worse than none — it converts one
    failing dependency into an outage of everything behind the same instance.
    """

    def __init__(
        self,
        name: str,
        config: BreakerConfig | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        on_state_change: Callable[[str, BreakerState, BreakerState], None] | None = None,
    ) -> None:
        self._name = name
        self._config = config or BreakerConfig()
        self._clock = clock
        """Injectable so the reset timeout is testable without sleeping. A test that waits 30
        real seconds is a test nobody runs on every commit."""
        self._on_state_change = on_state_change
        self._state = BreakerState.CLOSED
        self._window: deque[bool] = deque(maxlen=self._config.window_size)
        self._opened_at = 0.0
        self._half_open_calls = 0
        self._stats = BreakerStats(name=name, last_state_change=clock())
        self._lock = asyncio.Lock()
        """Guards state transitions only, never the call itself. Holding a lock across the
        guarded call would serialise every request through the dependency and turn the breaker
        into the bottleneck it exists to prevent."""

    @property
    def name(self) -> str:
        return self._name

    @property
    def state(self) -> BreakerState:
        return self._state

    @property
    def stats(self) -> BreakerStats:
        self._stats.state = self._state
        return self._stats

    @property
    def failure_rate(self) -> float:
        if len(self._window) < self._config.minimum_calls:
            return 0.0
        failures = sum(1 for ok in self._window if not ok)
        return failures / len(self._window)

    async def call(self, operation: Callable[[], Awaitable[T]]) -> T:
        """Run ``operation`` through the breaker.

        Raises :class:`BreakerOpenError` without attempting the call when the circuit is open,
        and ``TimeoutError`` when the call exceeds ``call_timeout_seconds``.
        """
        await self._before_call()
        began = self._clock()
        try:
            # `asyncio.timeout` rather than `wait_for`: on Python 3.11 `wait_for` wraps the
            # coroutine in a Task and schedules a timer for every call, and the benchmark
            # measured that at 132us — against 4us to reject one. A breaker whose closed path
            # costs thirty times its open path is adding load on the way to shedding it, and it
            # sits in front of every external call the platform makes.
            async with asyncio.timeout(self._config.call_timeout_seconds):
                result = await operation()
        except TimeoutError:
            self._stats.timeouts += 1
            await self._record(False, "timeout")
            raise
        except Exception as exc:
            await self._record(False, f"{type(exc).__name__}: {exc}")
            raise
        else:
            await self._record(True, "")
            _log.debug(
                "breaker.call_ok",
                breaker=self._name,
                duration_ms=round((self._clock() - began) * 1000, 2),
            )
            return result

    async def _before_call(self) -> None:
        async with self._lock:
            if self._state is BreakerState.OPEN:
                elapsed = self._clock() - self._opened_at
                if elapsed < self._config.reset_timeout_seconds:
                    self._stats.rejected += 1
                    raise BreakerOpenError(
                        self._name,
                        retry_after_seconds=self._config.reset_timeout_seconds - elapsed,
                    )
                self._transition(BreakerState.HALF_OPEN)
                self._half_open_calls = 0
                self._stats.consecutive_successes = 0

            if self._state is BreakerState.HALF_OPEN:
                if self._half_open_calls >= self._config.half_open_max_calls:
                    # Probes are in flight and the verdict is not in yet. Rejecting is correct:
                    # admitting unlimited traffic to a dependency that has just come back is how
                    # a recovering database is knocked over a second time.
                    self._stats.rejected += 1
                    raise BreakerOpenError(self._name, retry_after_seconds=0.0)
                self._half_open_calls += 1

            self._stats.calls += 1

    async def _record(self, ok: bool, error: str) -> None:
        async with self._lock:
            if ok:
                self._stats.successes += 1
            else:
                self._stats.failures += 1
                self._stats.last_error = error

            if self._state is BreakerState.HALF_OPEN:
                self._half_open_calls = max(0, self._half_open_calls - 1)
                if ok:
                    self._stats.consecutive_successes += 1
                    if self._stats.consecutive_successes >= self._config.success_threshold:
                        self._window.clear()
                        self._transition(BreakerState.CLOSED)
                else:
                    # One failure while probing reopens immediately, without waiting for the
                    # window to fill. The dependency has already told us it is not ready.
                    self._open()
                return

            self._window.append(ok)
            if self._state is BreakerState.CLOSED and self._should_open():
                self._open()

    def _should_open(self) -> bool:
        return (
            len(self._window) >= self._config.minimum_calls
            and self.failure_rate >= self._config.failure_threshold
        )

    def _open(self) -> None:
        self._opened_at = self._clock()
        self._stats.opened_count += 1
        self._stats.consecutive_successes = 0
        self._half_open_calls = 0
        self._transition(BreakerState.OPEN)

    def _transition(self, to: BreakerState) -> None:
        if to is self._state:
            return
        previous, self._state = self._state, to
        self._stats.state = to
        self._stats.last_state_change = self._clock()
        _log.warning(
            "breaker.state_change",
            breaker=self._name,
            **{"from": str(previous)},
            to=str(to),
            failure_rate=round(self.failure_rate, 3),
            last_error=self._stats.last_error,
        )
        if self._on_state_change is not None:
            self._on_state_change(self._name, previous, to)

    async def reset(self) -> None:
        """Force the circuit closed.

        For an operator who knows the dependency is healthy and does not want to wait out the
        reset timeout — a failover completed, a network partition healed. Clears the window too,
        so the pre-outage failures cannot immediately reopen it.
        """
        async with self._lock:
            self._window.clear()
            self._half_open_calls = 0
            self._stats.consecutive_successes = 0
            self._transition(BreakerState.CLOSED)

    async def trip(self, reason: str = "manual") -> None:
        """Force the circuit open. For draining a dependency before maintenance."""
        async with self._lock:
            self._stats.last_error = reason
            self._open()

    def health(self) -> dict[str, Any]:
        """What the health surface reports for this dependency."""
        return {
            "state": str(self._state),
            "failureRate": round(self.failure_rate, 3),
            "windowed": len(self._window),
            "rejected": self._stats.rejected,
            "openedCount": self._stats.opened_count,
            "lastError": self._stats.last_error,
        }


@dataclass(slots=True)
class BreakerRegistry:
    """Every breaker, one per dependency.

    Per-dependency isolation is the point. A registry that handed out one shared breaker would
    let a slow knowledge graph open the circuit in front of the operational database, converting
    a degraded feature into an outage — the exact failure a breaker exists to prevent.
    """

    configs: dict[str, BreakerConfig] = field(default_factory=dict)
    default_config: BreakerConfig = field(default_factory=BreakerConfig)
    _breakers: dict[str, CircuitBreaker] = field(default_factory=dict, init=False)
    on_state_change: Callable[[str, BreakerState, BreakerState], None] | None = None

    def get(self, name: str) -> CircuitBreaker:
        """The breaker for ``name``, created on first use."""
        breaker = self._breakers.get(name)
        if breaker is None:
            breaker = CircuitBreaker(
                name,
                self.configs.get(name, self.default_config),
                on_state_change=self.on_state_change,
            )
            self._breakers[name] = breaker
        return breaker

    async def call(self, name: str, operation: Callable[[], Awaitable[T]]) -> T:
        return await self.get(name).call(operation)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._breakers))

    def open_circuits(self) -> tuple[str, ...]:
        return tuple(sorted(n for n, b in self._breakers.items() if b.state is BreakerState.OPEN))

    async def reset_all(self) -> None:
        for breaker in self._breakers.values():
            await breaker.reset()

    def health(self) -> dict[str, Any]:
        """Every breaker's state, for the health surface.

        ``degraded`` rather than ``down`` when a circuit is open: an open circuit means one
        dependency is unavailable, and whether that makes the platform unserviceable is the
        criticality question the container already answers. Reporting it as down here would
        remove every replica from the load balancer over a degraded knowledge graph.
        """
        open_now = self.open_circuits()
        return {
            "status": "degraded" if open_now else "up",
            "open": list(open_now),
            "breakers": {name: b.health() for name, b in sorted(self._breakers.items())},
        }
