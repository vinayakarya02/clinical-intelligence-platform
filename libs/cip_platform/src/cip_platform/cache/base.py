"""Cache contract, keys, and domains.

One interface over five domains with different lifetimes and invalidation triggers
(docs/design/adr-0014-cache-topology.md).

The load-bearing detail is :class:`CacheKey`: **it cannot be constructed without a tenant.** A
cache is the easiest place in a multi-tenant system to leak data, because a key collision
produces a hit — no error, no log, just another tenant's answer returned quickly and
confidently. Making the tenant a required argument is the same constructor-level defence
``VectorQuery`` uses in Phase 2.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "Cache",
    "CacheDomain",
    "CacheKey",
    "CacheStats",
    "content_hash",
]


class CacheDomain(StrEnum):
    """What is being cached. Each has its own namespace, TTL, and invalidation trigger."""

    EMBEDDING = "embedding"
    RETRIEVAL = "retrieval"
    SESSION = "session"
    PROMPT = "prompt"
    GRAPH = "graph"

    @property
    def invalidated_by(self) -> str:
        """The event that makes entries in this domain stale.

        Documented on the enum so the sweep in :mod:`cip_platform.cache.domains` and the
        reason for it cannot drift apart.
        """
        return {
            "embedding": "embedding model version change",
            "retrieval": "document ingest for the tenant",
            "session": "session end or expiry",
            "prompt": "prompt deployment change",
            "graph": "graph write for the tenant",
        }[self.value]


def content_hash(*parts: Any) -> str:
    """Stable short digest of the parts that identify a cache entry.

    Truncated to 32 hex characters: full SHA-256 doubles key length for a collision
    probability that is already negligible, and Redis key length is real memory.
    """
    joined = "\x1f".join(str(part) for part in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True, slots=True)
class CacheKey:
    """A fully-qualified cache key.

    Rendered as ``cip:<domain>:<tenant>:<discriminator>``. The tenant sits ahead of the
    discriminator so a namespace sweep for one tenant is a prefix scan rather than a full
    keyspace walk.
    """

    domain: CacheDomain
    tenant_id: uuid.UUID
    discriminator: str

    def __post_init__(self) -> None:
        if not self.discriminator.strip():
            raise ValueError("CacheKey.discriminator must not be empty")

    def render(self) -> str:
        return f"cip:{self.domain.value}:{self.tenant_id}:{self.discriminator}"

    @classmethod
    def for_content(cls, domain: CacheDomain, tenant_id: uuid.UUID, *parts: Any) -> CacheKey:
        """Build a key from arbitrary identifying parts."""
        return cls(domain=domain, tenant_id=tenant_id, discriminator=content_hash(*parts))

    @staticmethod
    def namespace(domain: CacheDomain, tenant_id: uuid.UUID) -> str:
        """The prefix covering one tenant's entries in one domain, for invalidation."""
        return f"cip:{domain.value}:{tenant_id}:"


@dataclass(frozen=True, slots=True)
class CacheStats:
    """Hit-rate telemetry. A cache whose effectiveness is not measured is a guess."""

    hits: int = 0
    misses: int = 0
    evictions: int = 0
    errors: int = 0

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return round(self.hits / total, 4) if total else 0.0


@runtime_checkable
class Cache(Protocol):
    """Reads and writes cached values, scoped by key.

    Every method takes a :class:`CacheKey`, so there is no way to reach the backend with an
    unscoped string.
    """

    async def get(self, key: CacheKey) -> Any | None: ...

    async def set(self, key: CacheKey, value: Any, *, ttl_seconds: int) -> None: ...

    async def delete(self, key: CacheKey) -> bool: ...

    async def invalidate_namespace(self, domain: CacheDomain, tenant_id: uuid.UUID) -> int:
        """Drop every entry for one tenant in one domain. Returns the count removed."""
        ...

    def stats(self) -> CacheStats: ...
