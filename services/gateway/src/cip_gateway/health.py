"""Health endpoints.

Three, because Kubernetes asks three different questions and answering them with one endpoint
gets at least two of them wrong.

**live** — "is the process wedged?" It checks nothing external. A liveness probe that touches
the database restarts every replica during a database blip, turning a degradation into an
outage; that is the single most common way a well-intentioned probe causes the incident it
was meant to detect.

**ready** — "should this replica receive traffic?" It checks dependencies. A replica that
cannot reach its stores is removed from the Service without being killed, so it can recover.

**startup** — "has it finished booting?" Generous, so a slow first start is not a crash loop.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from cip_core.logging import get_logger

__all__ = ["DependencyCheck", "HealthReport", "HealthService"]

_log = get_logger(__name__)

#: A dependency check that takes longer than this is treated as failed. Without a cap, a
#: hanging check makes the readiness probe itself hang and the replica is killed by timeout
#: rather than removed from the Service.
_CHECK_TIMEOUT_SECONDS = 3.0


@dataclass(frozen=True, slots=True)
class DependencyCheck:
    """One named dependency and how to test it."""

    name: str
    probe: Callable[[], Awaitable[Any]]
    critical: bool = True
    """A non-critical dependency failing degrades the service without removing it from the
    Service. The cache is the example: unreachable Redis makes the system slower, and taking
    every replica out of rotation over it would turn a slowdown into an outage."""


@dataclass(frozen=True, slots=True)
class HealthReport:
    """The outcome of a readiness evaluation."""

    ready: bool
    checks: dict[str, str] = field(default_factory=dict)
    degraded: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "status": "ready" if self.ready else "not_ready",
            "checks": self.checks,
            "degraded": list(self.degraded),
        }


class HealthService:
    """Evaluates liveness and readiness."""

    def __init__(self, checks: list[DependencyCheck] | None = None) -> None:
        self._checks = list(checks or [])
        self._started_at = time.monotonic()

    def register(self, check: DependencyCheck) -> None:
        self._checks.append(check)

    def live(self) -> dict[str, Any]:
        """Process liveness. Deliberately checks nothing external."""
        return {"status": "live", "uptime_seconds": round(time.monotonic() - self._started_at, 1)}

    async def ready(self) -> HealthReport:
        """Dependency readiness."""
        import asyncio

        results: dict[str, str] = {}
        degraded: list[str] = []
        ready = True

        for check in self._checks:
            try:
                await asyncio.wait_for(check.probe(), timeout=_CHECK_TIMEOUT_SECONDS)
                results[check.name] = "ok"
            except TimeoutError:
                results[check.name] = "timeout"
                if check.critical:
                    ready = False
                else:
                    degraded.append(check.name)
            except Exception as exc:
                results[check.name] = f"error: {type(exc).__name__}"
                if check.critical:
                    ready = False
                else:
                    degraded.append(check.name)

        if not ready:
            _log.warning("health.not_ready", checks=results)
        return HealthReport(ready=ready, checks=results, degraded=tuple(degraded))
