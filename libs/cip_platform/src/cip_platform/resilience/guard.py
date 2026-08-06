"""One guarded path to every external dependency.

W7's requirement is that *every* external dependency uses a timeout, a retry policy, a circuit
breaker, structured logging, and a span. The way to make "every" true is to have one thing that
does all five, rather than five things applied consistently by discipline — discipline is what
fails on the call site added at 6pm on a Friday.

**Order matters, and this is the order:**

```
retry(  breaker(  timeout( operation )  )  )
```

- **timeout innermost.** A hang must be converted into a failure the breaker can count.
  Outside the breaker, a hanging call would never record an outcome and the circuit would never
  open on the failure mode that hurts most.
- **breaker inside retry.** Retrying a call the breaker rejected is the retry storm the breaker
  exists to prevent, so ``BreakerOpenError`` is *not* retried — it fails out immediately.
- **retry outermost**, so a transient blip is absorbed before it reaches the caller.

**Retries are only for transient failures, and the distinction is declared rather than guessed.**
Retrying a constraint violation or a malformed query never succeeds; it just multiplies load and
delays the error. ``retry_on`` names the exception types worth retrying and defaults to nothing —
a caller that wants retries says which failures deserve them.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

from cip_core.logging import get_logger
from cip_platform.resilience.breaker import BreakerOpenError, BreakerRegistry

__all__ = ["Guard", "GuardPolicy", "guarded"]

_log = get_logger(__name__)

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class GuardPolicy:
    """Retry and backoff for one dependency."""

    max_attempts: int = 3
    """Total attempts, not retries. 3 means the original call plus two retries."""

    base_delay_seconds: float = 0.2
    max_delay_seconds: float = 5.0

    jitter: bool = True
    """Full jitter on the backoff.

    ``cip_platform.tasks.backoff_seconds`` is deliberately deterministic because a broker already
    spreads redelivery and a test can assert on it. Here the opposite holds: several replicas
    retrying a shared dependency in lockstep produce a thundering herd that arrives exactly when
    the dependency is trying to recover. Jitter is the difference between a recovering database
    and one that is knocked over again on a two-second cycle.
    """

    retry_on: tuple[type[BaseException], ...] = ()
    """Exception types worth retrying. Empty means retry nothing."""

    def delay_for(self, attempt: int, *, rng: Callable[[], float] = random.random) -> float:
        """Backoff before ``attempt`` (1-based, so attempt 2 is the first retry)."""
        exponential = min(self.base_delay_seconds * (2 ** (attempt - 1)), self.max_delay_seconds)
        return exponential * rng() if self.jitter else exponential


@dataclass(slots=True)
class Guard:
    """Wraps calls to one named dependency in the full protection stack."""

    name: str
    breakers: BreakerRegistry
    policy: GuardPolicy = field(default_factory=GuardPolicy)
    tracer: Any = None
    """An OpenTelemetry tracer when one is configured, None otherwise. Typed loosely on purpose:
    W4 wires OpenTelemetry, and W7 must not take a hard dependency on a library that is not
    installed yet. When it is None the span is simply not started, and nothing else changes."""

    async def call(
        self,
        operation: Callable[[], Awaitable[T]],
        *,
        operation_name: str = "",
    ) -> T:
        """Run ``operation`` with timeout, breaker, retry, logging, and a span."""
        label = operation_name or self.name
        breaker = self.breakers.get(self.name)
        last: BaseException | None = None

        for attempt in range(1, self.policy.max_attempts + 1):
            began = time.perf_counter()
            try:
                result = await self._traced(breaker.call(operation), label, attempt)
            except BreakerOpenError:
                # Never retried. Retrying a rejected call is precisely the storm the breaker was
                # opened to stop.
                _log.warning(
                    "guard.rejected", dependency=self.name, operation=label, attempt=attempt
                )
                if last is not None:
                    # The circuit opened *during* this call's own retries: attempt 1 failed for
                    # a real reason, that failure tripped the breaker, and attempt 2 was
                    # rejected. Surfacing BreakerOpenError here would replace a diagnosis
                    # ("connection refused") with a symptom ("we did not try"), and the caller
                    # would have no way to learn why. Raise what actually failed.
                    raise last from None
                raise
            except self.policy.retry_on as exc:
                last = exc
                elapsed = (time.perf_counter() - began) * 1000
                if attempt >= self.policy.max_attempts:
                    _log.error(
                        "guard.exhausted",
                        dependency=self.name,
                        operation=label,
                        attempts=attempt,
                        error=type(exc).__name__,
                    )
                    raise
                delay = self.policy.delay_for(attempt)
                _log.warning(
                    "guard.retrying",
                    dependency=self.name,
                    operation=label,
                    attempt=attempt,
                    duration_ms=round(elapsed, 2),
                    delay_s=round(delay, 3),
                    error=type(exc).__name__,
                )
                await asyncio.sleep(delay)
            else:
                if attempt > 1:
                    _log.info(
                        "guard.recovered",
                        dependency=self.name,
                        operation=label,
                        attempts=attempt,
                    )
                return result

        raise last if last is not None else RuntimeError("unreachable")

    async def _traced(self, awaitable: Awaitable[T], label: str, attempt: int) -> T:
        """Run inside a span when a tracer is configured.

        Attribute names follow the OpenTelemetry conventions this platform will adopt in W4;
        they are set here so the span is already shaped correctly when the exporter arrives.
        """
        if self.tracer is None:
            return await awaitable
        with self.tracer.start_as_current_span(f"{self.name}.{label}") as span:
            span.set_attribute("cip.dependency", self.name)
            span.set_attribute("cip.operation", label)
            span.set_attribute("cip.attempt", attempt)
            try:
                return await awaitable
            except Exception as exc:
                span.set_attribute("error.type", type(exc).__name__)
                raise


def guarded(
    breakers: BreakerRegistry,
    name: str,
    *,
    policy: GuardPolicy | None = None,
    tracer: Any = None,
) -> Guard:
    """A guard for one dependency."""
    return Guard(name=name, breakers=breakers, policy=policy or GuardPolicy(), tracer=tracer)
