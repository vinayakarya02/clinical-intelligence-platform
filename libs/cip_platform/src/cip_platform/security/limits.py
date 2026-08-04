"""Rate limiting and spend budgets.

Two independent controls, because they bound different quantities
(docs/design/adr-0018-cost-governance.md). A rate limit bounds *requests*; for a system that
pays a provider per token, a hundred cheap requests and a hundred carrying a 200-page discharge
summary look identical to a token bucket and differ by orders of magnitude in cost. Only a
budget bounds spend.

Both are per tenant *and* per principal. Per-principal matters on its own: one leaked API key
must not be able to consume its whole tenant's allowance.
"""

from __future__ import annotations

import datetime as dt
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from enum import StrEnum

from cip_core.errors import CipError
from cip_core.logging import get_logger

__all__ = [
    "BudgetDecision",
    "BudgetState",
    "RateLimitError",
    "SpendBudget",
    "TokenBucket",
    "TokenBucketLimiter",
]

_log = get_logger(__name__)


class RateLimitError(CipError):
    """The caller exceeded a limit.

    Carries ``retry_after_seconds`` so the response can set ``Retry-After``. A 429 without one
    invites an immediate retry, which is how a rate limit becomes a load amplifier.
    """

    status = 429
    problem_type = "rate-limit-exceeded"
    title = "Rate limit exceeded"

    def __init__(self, message: str, *, retry_after_seconds: float, scope: str) -> None:
        super().__init__(message)
        self.retry_after_seconds = max(1.0, retry_after_seconds)
        self.scope = scope


@dataclass(slots=True)
class TokenBucket:
    """A refilling allowance.

    Chosen over a fixed window because a fixed window lets a caller spend the whole allowance
    in the last instant of one window and again in the first instant of the next — twice the
    intended rate across the boundary. A bucket refills continuously and has no boundary.
    """

    capacity: float
    refill_per_second: float
    tokens: float
    updated_at: float

    def consume(self, amount: float, now: float) -> tuple[bool, float]:
        """Take ``amount`` if available. Returns ``(allowed, retry_after_seconds)``."""
        elapsed = max(0.0, now - self.updated_at)
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_second)
        self.updated_at = now

        if self.tokens >= amount:
            self.tokens -= amount
            return True, 0.0

        deficit = amount - self.tokens
        return False, deficit / self.refill_per_second if self.refill_per_second else 60.0


class TokenBucketLimiter:
    """Per-key token buckets with an LRU bound.

    The bound matters: a limiter keyed by principal accumulates one bucket per key that has
    ever called, so an unbounded map is a slow memory leak driven by exactly the traffic the
    limiter exists to handle.
    """

    def __init__(
        self,
        *,
        requests_per_minute: int,
        burst_multiplier: float = 2.0,
        max_buckets: int = 10_000,
        clock: object = time.monotonic,
    ) -> None:
        if requests_per_minute < 1:
            raise ValueError("requests_per_minute must be >= 1")
        self._rate = requests_per_minute / 60.0
        self._capacity = requests_per_minute * burst_multiplier
        self._max_buckets = max_buckets
        self._clock = clock
        # Insertion-ordered so eviction is O(1). A linear scan for the oldest bucket would
        # make every request O(n) once the map is full, and an attacker rotating principal
        # identifiers could then force a 10,000-element scan per request — turning the
        # control that exists to prevent resource exhaustion into a way to cause it.
        self._buckets: OrderedDict[str, TokenBucket] = OrderedDict()

    def check(self, key: str, *, scope: str, cost: float = 1.0) -> None:
        """Consume one unit for ``key``, raising :class:`RateLimitError` if exhausted."""
        now = float(self._clock())  # type: ignore[operator]
        bucket = self._buckets.get(key)
        if bucket is None:
            while len(self._buckets) >= self._max_buckets:
                self._buckets.popitem(last=False)
            bucket = TokenBucket(
                capacity=self._capacity,
                refill_per_second=self._rate,
                tokens=self._capacity,
                updated_at=now,
            )
            self._buckets[key] = bucket
        else:
            self._buckets.move_to_end(key)

        allowed, retry_after = bucket.consume(cost, now)
        if not allowed:
            _log.info("ratelimit.rejected", scope=scope, retry_after=round(retry_after, 2))
            raise RateLimitError(
                f"Rate limit exceeded for {scope}",
                retry_after_seconds=retry_after,
                scope=scope,
            )

    def remaining(self, key: str) -> float:
        bucket = self._buckets.get(key)
        return bucket.tokens if bucket else self._capacity

    def tracked_keys(self) -> int:
        return len(self._buckets)


class BudgetDecision(StrEnum):
    """What a budget check concluded."""

    ALLOW = "allow"
    ALERT = "alert"
    REJECT = "reject"


@dataclass(slots=True)
class BudgetState:
    """Accumulated spend for one tenant in one window."""

    tenant_id: uuid.UUID
    window_start: dt.date
    spent_usd: float = 0.0
    alerted: bool = False

    def reset_if_stale(self, today: dt.date) -> None:
        if self.window_start != today:
            self.window_start = today
            self.spent_usd = 0.0
            self.alerted = False


class SpendBudget:
    """Daily USD budget per tenant, with an alert threshold and a hard limit.

    Cost is charged **after** a request completes, from the token usage the answer reports,
    because the true cost is not knowable in advance. The check therefore tests *accumulated*
    spend: a request that crosses the limit is allowed to finish and the next one is refused.
    That overshoots by at most one request, which is the documented trade against a reservation
    protocol with compensation on failure (ADR-0018).
    """

    def __init__(
        self,
        *,
        daily_limit_usd: float,
        alert_ratio: float = 0.8,
        today: object = None,
    ) -> None:
        if daily_limit_usd < 0:
            raise ValueError("daily_limit_usd must be >= 0")
        if not 0.0 < alert_ratio < 1.0:
            raise ValueError("alert_ratio must be strictly between 0 and 1")
        self._limit = daily_limit_usd
        self._alert_ratio = alert_ratio
        self._today = today or (lambda: dt.datetime.now(dt.UTC).date())
        self._states: dict[uuid.UUID, BudgetState] = {}

    @property
    def enabled(self) -> bool:
        """A zero limit disables the control entirely."""
        return self._limit > 0

    def _state(self, tenant_id: uuid.UUID) -> BudgetState:
        today = self._today()  # type: ignore[operator]
        state = self._states.get(tenant_id)
        if state is None:
            state = BudgetState(tenant_id=tenant_id, window_start=today)
            self._states[tenant_id] = state
        else:
            state.reset_if_stale(today)
        return state

    def check(self, tenant_id: uuid.UUID) -> BudgetDecision:
        """Decide whether this tenant may make another request."""
        if not self.enabled:
            return BudgetDecision.ALLOW
        state = self._state(tenant_id)
        if state.spent_usd >= self._limit:
            return BudgetDecision.REJECT
        if state.spent_usd >= self._limit * self._alert_ratio:
            return BudgetDecision.ALERT
        return BudgetDecision.ALLOW

    def charge(self, tenant_id: uuid.UUID, cost_usd: float) -> BudgetDecision:
        """Record spend after a request, returning the decision for the *next* one."""
        if cost_usd < 0:
            raise ValueError("cost_usd must be >= 0")
        if not self.enabled:
            return BudgetDecision.ALLOW

        state = self._state(tenant_id)
        state.spent_usd += cost_usd
        decision = self.check(tenant_id)

        if decision is BudgetDecision.ALERT and not state.alerted:
            # Once per window: an alert on every request past the threshold is a page storm
            # that gets the alert muted, which is worse than no alert.
            state.alerted = True
            _log.warning(
                "budget.alert",
                tenant=str(tenant_id),
                spent_usd=round(state.spent_usd, 4),
                limit_usd=self._limit,
            )
        elif decision is BudgetDecision.REJECT:
            _log.error(
                "budget.exhausted",
                tenant=str(tenant_id),
                spent_usd=round(state.spent_usd, 4),
                limit_usd=self._limit,
            )
        return decision

    def spent(self, tenant_id: uuid.UUID) -> float:
        return self._state(tenant_id).spent_usd

    def seconds_until_reset(self) -> float:
        """Until the daily window rolls over, for ``Retry-After``.

        Pointing a client at the actual reset boundary rather than a fixed backoff means it
        retries when budget will exist, instead of retrying into a wall until then.
        """
        now = dt.datetime.now(dt.UTC)
        tomorrow = dt.datetime.combine(
            now.date() + dt.timedelta(days=1), dt.time.min, tzinfo=dt.UTC
        )
        return (tomorrow - now).total_seconds()
