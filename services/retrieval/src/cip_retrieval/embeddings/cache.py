"""Embedding cache.

Embeddings are pure functions of (text, model), which makes them ideally cacheable — and
worth caching, because they are the dominant cost of re-processing a corpus. Re-chunking
after a strategy change re-embeds mostly-identical text; without a cache that run pays full
price for content that has not changed.

Two implementations, both justified: :class:`InMemoryEmbeddingCache` bounds a single
process (batch ingest, tests) and :class:`NullEmbeddingCache` disables caching without
forcing null checks into the service. A shared Redis cache is the obvious third — deferred
until there is more than one process to share between.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Protocol, runtime_checkable

__all__ = ["EmbeddingCache", "InMemoryEmbeddingCache", "NullEmbeddingCache"]


@runtime_checkable
class EmbeddingCache(Protocol):
    """Stores vectors keyed by content and model."""

    async def get(self, key: str) -> tuple[float, ...] | None: ...

    async def set(self, key: str, vector: tuple[float, ...]) -> None: ...


class NullEmbeddingCache:
    """Caches nothing.

    Exists so :class:`~cip_retrieval.embeddings.service.EmbeddingService` can always call a
    cache rather than branching on whether one is configured.
    """

    async def get(self, key: str) -> tuple[float, ...] | None:
        return None

    async def set(self, key: str, vector: tuple[float, ...]) -> None:
        return None


class InMemoryEmbeddingCache:
    """LRU cache bounded by entry count.

    Bounded by entries rather than bytes: vectors from one model are a fixed width, so
    entries are a direct proxy for memory and a far cheaper thing to measure.

    The cost per entry is dominated by Python object overhead, not by the float data. A
    384-dimension tuple measures ~12.4 KB resident (``tracemalloc``), roughly four times the
    12 KB the raw doubles would suggest, because every element is a boxed ``float``. The
    default of 10,000 entries is therefore ~125 MB — chosen to be a defensible footprint for
    a long-lived service process. A batch ingest should raise it deliberately and size the
    container to match; the previous 50,000 default was ~600 MB, documented as 150 MB, which
    is the kind of arithmetic that turns into an OOM kill nobody can explain.
    """

    def __init__(self, max_entries: int = 10_000) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be >= 1")
        self._max_entries = max_entries
        self._entries: OrderedDict[str, tuple[float, ...]] = OrderedDict()
        self._hits = 0
        self._misses = 0

    async def get(self, key: str) -> tuple[float, ...] | None:
        vector = self._entries.get(key)
        if vector is None:
            self._misses += 1
            return None
        self._entries.move_to_end(key)
        self._hits += 1
        return vector

    async def set(self, key: str, vector: tuple[float, ...]) -> None:
        if key in self._entries:
            self._entries.move_to_end(key)
        self._entries[key] = vector
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)

    @property
    def stats(self) -> dict[str, int | float]:
        """Hit-rate telemetry. A cache whose effectiveness is not measured is a guess."""
        total = self._hits + self._misses
        return {
            "entries": len(self._entries),
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(self._hits / total, 4) if total else 0.0,
        }
