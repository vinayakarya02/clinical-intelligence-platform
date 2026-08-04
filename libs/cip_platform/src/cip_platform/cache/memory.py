"""In-process cache with TTL and LRU eviction.

Not a mock. It implements the full :class:`Cache` contract with the same TTL and namespace
semantics as the Redis backend, so cache-related bugs — a missing tenant scope, a wrong TTL,
an invalidation that misses — fail in CI rather than only under load. It is the right backend
for tests and single-process development, and ``PlatformSettings`` refuses it in deployed
environments because a per-replica cache's hit rate falls as you scale out.
"""

from __future__ import annotations

import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from cip_core.logging import get_logger
from cip_platform.cache.base import CacheDomain, CacheKey, CacheStats

__all__ = ["InMemoryCache"]

_log = get_logger(__name__)


@dataclass(slots=True)
class _Entry:
    value: Any
    expires_at: float

    def is_live(self, now: float) -> bool:
        return now < self.expires_at


class InMemoryCache:
    """LRU cache with per-entry expiry."""

    def __init__(self, *, max_entries: int = 10_000, clock: Any = time.monotonic) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be >= 1")
        self._max_entries = max_entries
        self._clock = clock
        """Injectable so TTL behaviour is testable without sleeping. A test that waits for a
        real second is a test nobody runs on every commit."""
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self._hits = 0
        self._misses = 0
        self._evictions = 0

    async def get(self, key: CacheKey) -> Any | None:
        rendered = key.render()
        entry = self._entries.get(rendered)
        if entry is None:
            self._misses += 1
            return None
        if not entry.is_live(self._clock()):
            # Expire lazily rather than sweeping. A background sweeper is another failure mode
            # for a bounded cache that already evicts under pressure.
            del self._entries[rendered]
            self._misses += 1
            return None
        self._entries.move_to_end(rendered)
        self._hits += 1
        return entry.value

    async def set(self, key: CacheKey, value: Any, *, ttl_seconds: int) -> None:
        if ttl_seconds < 1:
            raise ValueError("ttl_seconds must be >= 1")
        rendered = key.render()
        self._entries[rendered] = _Entry(value=value, expires_at=self._clock() + ttl_seconds)
        self._entries.move_to_end(rendered)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)
            self._evictions += 1

    async def delete(self, key: CacheKey) -> bool:
        return self._entries.pop(key.render(), None) is not None

    async def invalidate_namespace(self, domain: CacheDomain, tenant_id: uuid.UUID) -> int:
        prefix = CacheKey.namespace(domain, tenant_id)
        doomed = [k for k in self._entries if k.startswith(prefix)]
        for key in doomed:
            del self._entries[key]
        if doomed:
            _log.debug(
                "cache.namespace_invalidated",
                domain=str(domain),
                removed=len(doomed),
                reason=domain.invalidated_by,
            )
        return len(doomed)

    def stats(self) -> CacheStats:
        return CacheStats(hits=self._hits, misses=self._misses, evictions=self._evictions)

    def clear(self) -> None:
        """Test helper; not part of the protocol."""
        self._entries.clear()
