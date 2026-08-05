"""Choosing a cache backend from settings.

`RedisCache` was written in Phase 4 and instantiated nowhere. `PlatformSettings` refuses
``backend = "memory"`` in a deployed environment, so production configuration named a backend
that no code path could build — the configuration described a system nobody had wired.

This is that wire. It is deliberately the only place in the codebase that decides which cache
exists, because two places deciding is how a deployment ends up with one answer in the request
path and a different one in a background job.

**The client is built here, not by the cache.** `RedisCache` takes an already-constructed client
precisely so connection pooling, TLS, and failover stay deployment concerns. This module owns
that construction and nothing else does.
"""

from __future__ import annotations

from typing import Any

from cip_platform.cache.base import Cache
from cip_platform.cache.memory import InMemoryCache
from cip_platform.cache.redis_cache import RedisCache
from cip_platform.config import CachePolicy

__all__ = ["CacheBackendError", "build_cache", "build_redis_client"]


class CacheBackendError(RuntimeError):
    """The configured cache backend could not be built."""


def build_redis_client(url: str, *, decode_responses: bool = False) -> Any:
    """An async Redis client from a URL.

    Imported lazily so a process that never touches Redis — every unit test, the in-memory
    development path — does not pay the import, and so a missing optional dependency surfaces
    as a clear message here rather than as an ImportError at module load in an unrelated file.
    """
    try:
        from redis.asyncio import Redis
    except ImportError as exc:  # pragma: no cover - dependency is declared in pyproject
        raise CacheBackendError(
            "the 'redis' package is required for the redis backend; it is a declared "
            "dependency, so this means the environment is incomplete"
        ) from exc

    if not url.strip():
        raise CacheBackendError("a redis URL is required and none was configured")

    # health_check_interval revalidates an idle pooled connection before handing it out. Managed
    # Redis and cloud load balancers drop idle TCP connections silently, and without this the
    # first request after a quiet period fails rather than reconnecting.
    return Redis.from_url(
        url,
        decode_responses=decode_responses,
        health_check_interval=30,
        socket_connect_timeout=5,
        socket_keepalive=True,
    )


def build_cache(policy: CachePolicy) -> Cache:
    """The cache this configuration asks for.

    Raises rather than falling back. A cache that silently degrades to per-process memory when
    Redis is misconfigured produces a hit rate that falls as replicas are added — which presents
    as a capacity problem and is diagnosed as one, often for a long time.
    """
    if policy.backend == "memory":
        return InMemoryCache(max_entries=policy.max_entries)
    if policy.backend == "redis":
        return RedisCache(build_redis_client(policy.redis_url))
    raise CacheBackendError(f"unknown cache backend {policy.backend!r}")
