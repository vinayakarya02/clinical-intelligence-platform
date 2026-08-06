"""Resilience primitives: circuit breakers and the guarded call path.

Retry without a breaker is a retry storm; a breaker without a timeout cannot open on a hang.
These belong together, so they live together.
"""

from cip_platform.resilience.breaker import (
    BreakerConfig,
    BreakerOpenError,
    BreakerRegistry,
    BreakerState,
    BreakerStats,
    CircuitBreaker,
)
from cip_platform.resilience.guard import Guard, GuardPolicy, guarded

__all__ = [
    "BreakerConfig",
    "BreakerOpenError",
    "BreakerRegistry",
    "BreakerState",
    "BreakerStats",
    "CircuitBreaker",
    "Guard",
    "GuardPolicy",
    "guarded",
]
