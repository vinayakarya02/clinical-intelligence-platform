"""Caching: five domains, one interface, a tenant in every key."""

from cip_platform.cache.base import (
    Cache,
    CacheDomain,
    CacheKey,
    CacheStats,
    content_hash,
)
from cip_platform.cache.domains import (
    CachedEmbeddingProvider,
    CacheDomains,
    build_domains,
)
from cip_platform.cache.factory import CacheBackendError, build_cache, build_redis_client
from cip_platform.cache.memory import InMemoryCache
from cip_platform.cache.redis_cache import RedisCache

__all__ = [
    "Cache",
    "CacheBackendError",
    "CacheDomain",
    "CacheDomains",
    "CacheKey",
    "CacheStats",
    "CachedEmbeddingProvider",
    "InMemoryCache",
    "RedisCache",
    "build_cache",
    "build_domains",
    "build_redis_client",
    "content_hash",
]
