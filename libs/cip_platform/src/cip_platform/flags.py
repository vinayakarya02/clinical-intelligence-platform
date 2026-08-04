"""Feature flags.

Deliberately small. A flag system that supports arbitrary targeting rules becomes a second,
untested control plane; this one supports the three modes a platform of this size actually
needs — off, on, and a stable percentage rollout — and nothing else.

Percentage rollout hashes the *tenant*, so a tenant either has a feature or does not, and
never flickers between requests. A clinician watching the assistant change behaviour between
two questions has no way to tell a rollout from a malfunction — the same reasoning as
session-stable prompt experiments in Phase 3.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass

from cip_core.logging import get_logger

__all__ = ["FeatureFlag", "FeatureFlags"]

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class FeatureFlag:
    """One flag."""

    name: str
    enabled: bool = False
    rollout_percent: int = 0
    """0 means "use ``enabled`` for everyone". 1-99 means a stable per-tenant split. 100 is
    equivalent to enabled."""

    description: str = ""

    def __post_init__(self) -> None:
        if not 0 <= self.rollout_percent <= 100:
            raise ValueError("rollout_percent must be in [0, 100]")

    def is_on_for(self, tenant_id: uuid.UUID | None) -> bool:
        if self.rollout_percent <= 0:
            return self.enabled
        if self.rollout_percent >= 100:
            return True
        if tenant_id is None:
            # No tenant means no stable bucket. Defaulting to off keeps an unauthenticated or
            # background code path out of a partial rollout rather than randomly inside it.
            return False
        digest = hashlib.sha256(f"{self.name}:{tenant_id}".encode()).digest()
        bucket = int.from_bytes(digest[:4], "big") % 100
        return bucket < self.rollout_percent


class FeatureFlags:
    """A flag set, resolved per tenant."""

    def __init__(self, flags: dict[str, FeatureFlag] | None = None) -> None:
        self._flags = dict(flags or {})

    @classmethod
    def from_config(cls, raw: dict[str, bool]) -> FeatureFlags:
        return cls({name: FeatureFlag(name=name, enabled=on) for name, on in raw.items()})

    def register(self, flag: FeatureFlag) -> None:
        self._flags[flag.name] = flag

    def is_on(self, name: str, *, tenant_id: uuid.UUID | None = None) -> bool:
        """Whether ``name`` is on. An unknown flag is off.

        Unknown-is-off rather than raising: a flag removed from configuration before its last
        call site should degrade to the pre-feature behaviour, not to a 500.
        """
        flag = self._flags.get(name)
        if flag is None:
            return False
        return flag.is_on_for(tenant_id)

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._flags))
