"""One way to say "this backing service is not here", and a record of every time it was said.

Written in Phase 9 W6. The guard in ``tests/conftest.py`` has to know which services a run never
reached, and the first version worked it out by matching phrases in skip messages. That is the
same shape of defect as everything else this workstream found: a correspondence between two
files that nothing owns. Reword a skip reason and the guard silently stops noticing that whole
service — a regression that makes the suite *quieter*, which is the kind nobody reports.

So the fixtures call :func:`absent` and it does both jobs at once: it formats the reason and it
records the service. There is no second place to keep in step.

The three-way split matters more than the bookkeeping:

- :func:`unconfigured` — no connection string. Skip, and remember which service.
- :func:`absent` — configured but unreachable. Skip, and remember which service.
- anything else — a **failure**. A permission error, a missing table, a broken fixture: these
  are misconfigured environments, not missing ones, and a suite that skips them reports success
  for work it never did. That is exactly how this suite stayed green while running nothing.
"""

from __future__ import annotations

import os

import pytest

__all__ = ["KAFKA", "MONGO", "NEO4J", "POSTGRES", "REDIS", "absent", "unconfigured"]

POSTGRES = "PostgreSQL"
REDIS = "Redis"
NEO4J = "Neo4j"
MONGO = "MongoDB"
KAFKA = "Kafka"

#: Services a fixture could not reach during this session. Read once, at the end of the run.
_UNREACHED: set[str] = set()


def unconfigured(service: str, variable: str) -> None:
    """Skip because nothing points at the service. Never returns."""
    _UNREACHED.add(service)
    pytest.skip(f"{variable} is unset — {service} behaviour is UNVERIFIED")


def absent(service: str, exc: BaseException, *, where: str = "") -> None:
    """Skip because the service is configured but unreachable. Never returns.

    The exception is quoted rather than summarised. "PostgreSQL is not reachable" is true of a
    stopped container, a wrong port, a refused password, and an expired certificate, and which
    one it is decides what you do next.
    """
    _UNREACHED.add(service)
    location = f" at {where}" if where else ""
    pytest.skip(f"{service} is unreachable{location}: {type(exc).__name__}: {exc}")


def unreachable_services() -> list[str]:
    """Every service a fixture reported missing, sorted. Empty when everything was reached."""
    return sorted(_UNREACHED)


def integration_enabled() -> bool:
    return os.environ.get("CIP_RUN_INTEGRATION") == "1"
