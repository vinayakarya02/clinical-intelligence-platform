"""Redis-backed cache.

The production backend. Three decisions differ from the obvious implementation and each closes
a failure that would otherwise be silent.

**A cache failure is never a request failure.** Redis being unreachable makes the system
slower, not broken, so every operation degrades to a miss and increments an error counter. The
counter matters as much as the degradation: a cache that has silently stopped working looks
exactly like a cache with a bad hit rate, and only the error count distinguishes them.

**Invalidation uses ``SCAN``, never ``KEYS``.** ``KEYS`` blocks the Redis event loop for the
duration of a full keyspace walk, so the one operation intended to keep clinical data fresh
would stall every other tenant's reads.

**Values are JSON, not pickle.** A cache is a deserialisation boundary reachable by anything
that can write to Redis, and ``pickle`` there is remote code execution. JSON cannot round-trip
every Python object, which is a real limitation and the right trade.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from cip_core.logging import get_logger
from cip_platform.cache.base import CacheDomain, CacheKey, CacheStats

__all__ = ["RedisCache"]

_log = get_logger(__name__)

#: Keys deleted per SCAN batch. Large enough to make progress, small enough that one batch
#: cannot become a long-running command.
_SCAN_BATCH = 500


class RedisCache:
    """Cache over a Redis client.

    Takes an already-constructed async client rather than a URL: connection pooling, TLS, and
    failover are deployment concerns that belong to whatever builds the client, and a cache
    that owns its own connection is a cache that cannot participate in them.
    """

    def __init__(self, client: Any, *, fail_open: bool = True) -> None:
        self._client = client
        self._fail_open = fail_open
        """When true, a backend error is a miss. False is for tests that need the error to
        surface; no production deployment should turn a cache outage into an outage."""
        self._hits = 0
        self._misses = 0
        self._errors = 0

    async def get(self, key: CacheKey) -> Any | None:
        try:
            raw = await self._client.get(key.render())
        except Exception as exc:
            return self._degrade("get", exc)

        if raw is None:
            self._misses += 1
            return None
        try:
            value = json.loads(raw)
        except (TypeError, ValueError) as exc:
            # A corrupt entry is worse than a missing one: it will keep being returned until
            # its TTL expires. Drop it so the next request repopulates.
            self._errors += 1
            self._misses += 1
            _log.warning("cache.corrupt_entry", key=key.render(), error=type(exc).__name__)
            await self.delete(key)
            return None
        self._hits += 1
        return value

    async def set(self, key: CacheKey, value: Any, *, ttl_seconds: int) -> None:
        if ttl_seconds < 1:
            raise ValueError("ttl_seconds must be >= 1")
        try:
            payload = json.dumps(value, separators=(",", ":"), default=str)
        except (TypeError, ValueError) as exc:
            # Refusing to cache is correct; the caller's operation already succeeded.
            self._errors += 1
            _log.warning("cache.unserialisable", key=key.render(), error=type(exc).__name__)
            return
        try:
            await self._client.set(key.render(), payload, ex=ttl_seconds)
        except Exception as exc:
            self._degrade("set", exc)

    async def delete(self, key: CacheKey) -> bool:
        try:
            return bool(await self._client.delete(key.render()))
        except Exception as exc:
            self._degrade("delete", exc)
            return False

    async def invalidate_namespace(self, domain: CacheDomain, tenant_id: uuid.UUID) -> int:
        """Sweep one tenant's entries in one domain, in batches.

        Batched deletion rather than one large `DEL`: a tenant with a big retrieval namespace
        would otherwise produce a single command holding the event loop for the whole sweep.
        """
        pattern = CacheKey.namespace(domain, tenant_id) + "*"
        removed = 0
        try:
            batch: list[str] = []
            async for raw in self._client.scan_iter(match=pattern, count=_SCAN_BATCH):
                batch.append(raw if isinstance(raw, str) else raw.decode())
                if len(batch) >= _SCAN_BATCH:
                    removed += int(await self._client.delete(*batch))
                    batch.clear()
            if batch:
                removed += int(await self._client.delete(*batch))
        except Exception as exc:
            self._degrade("invalidate", exc)
            return removed

        _log.info(
            "cache.namespace_invalidated",
            domain=str(domain),
            removed=removed,
            reason=domain.invalidated_by,
        )
        return removed

    def stats(self) -> CacheStats:
        return CacheStats(hits=self._hits, misses=self._misses, errors=self._errors)

    def _degrade(self, operation: str, exc: Exception) -> None:
        """Record a backend failure and either swallow it or re-raise."""
        self._errors += 1
        self._misses += 1
        _log.warning("cache.backend_error", operation=operation, error=type(exc).__name__)
        if not self._fail_open:
            raise
        return
