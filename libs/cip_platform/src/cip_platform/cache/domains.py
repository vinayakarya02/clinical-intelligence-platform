"""The five cache domains, and the adapters that let existing code use them unchanged.

Each domain owns its key shape and TTL; the shared interface owns everything else. The
adapters matter as much as the caches: :class:`CachedEmbeddingProvider` satisfies Phase 2's
existing ``EmbeddingCache`` protocol, so distributed caching becomes a *wiring* change in the
composition root rather than an edit to the retrieval service
(docs/design/adr-0013-platform-library-boundary.md).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from cip_core.logging import get_logger
from cip_platform.cache.base import Cache, CacheDomain, CacheKey
from cip_platform.config import CachePolicy

__all__ = [
    "CachedEmbeddingProvider",
    "EmbeddingCacheDomain",
    "GraphCacheDomain",
    "PromptCacheDomain",
    "RetrievalCacheDomain",
    "SessionCacheDomain",
    "build_domains",
]

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class _DomainBase:
    """Shared plumbing: a backend, a domain, and a TTL."""

    cache: Cache
    ttl_seconds: int

    @property
    def domain(self) -> CacheDomain:  # pragma: no cover - overridden by every subclass
        raise NotImplementedError

    async def invalidate_for_tenant(self, tenant_id: uuid.UUID) -> int:
        return await self.cache.invalidate_namespace(self.domain, tenant_id)


@dataclass(frozen=True, slots=True)
class EmbeddingCacheDomain(_DomainBase):
    """Vectors, keyed by model and content.

    Tenant-scoped even though embeddings of identical text are mathematically identical. A
    shared entry would be *correct* and would still mean one tenant's cache hit reveals that
    another tenant holds that exact clinical text — a timing side channel over PHI
    (ADR-0014).
    """

    @property
    def domain(self) -> CacheDomain:
        return CacheDomain.EMBEDDING

    def key(self, tenant_id: uuid.UUID, *, model_key: str, content_key: str) -> CacheKey:
        return CacheKey.for_content(self.domain, tenant_id, model_key, content_key)

    async def get_vector(
        self, tenant_id: uuid.UUID, *, model_key: str, content_key: str
    ) -> tuple[float, ...] | None:
        raw = await self.cache.get(
            self.key(tenant_id, model_key=model_key, content_key=content_key)
        )
        if raw is None:
            return None
        # JSON round-trips a tuple to a list; the protocol promises a tuple, and a caller
        # zipping a list onto vectors would work until something checked the type.
        return tuple(float(v) for v in raw)

    async def set_vector(
        self,
        tenant_id: uuid.UUID,
        *,
        model_key: str,
        content_key: str,
        vector: tuple[float, ...],
    ) -> None:
        await self.cache.set(
            self.key(tenant_id, model_key=model_key, content_key=content_key),
            list(vector),
            ttl_seconds=self.ttl_seconds,
        )


@dataclass(frozen=True, slots=True)
class RetrievalCacheDomain(_DomainBase):
    """Retrieval results, keyed by query and filters.

    The shortest TTL of the five, and the only one invalidated by ordinary write traffic: a
    stale retrieval result for a patient whose record just changed is a clinical error, not a
    quality regression.
    """

    @property
    def domain(self) -> CacheDomain:
        return CacheDomain.RETRIEVAL

    def key(
        self,
        tenant_id: uuid.UUID,
        *,
        query: str,
        top_k: int,
        patient_id: uuid.UUID | None = None,
        filters: dict[str, Any] | None = None,
    ) -> CacheKey:
        # Filters participate in the key. Two queries differing only by a document-type filter
        # return different results, and omitting the filter would serve one for the other.
        normalised = sorted((filters or {}).items())
        return CacheKey.for_content(
            self.domain, tenant_id, query.strip().casefold(), top_k, patient_id, normalised
        )


@dataclass(frozen=True, slots=True)
class SessionCacheDomain(_DomainBase):
    """Conversation state, keyed by session.

    Holds PHI-adjacent conversational content, so its TTL is a retention decision rather than
    a performance one — which is why it is short and why expiry is not configurable per
    request.
    """

    @property
    def domain(self) -> CacheDomain:
        return CacheDomain.SESSION

    def key(self, tenant_id: uuid.UUID, *, session_id: str) -> CacheKey:
        return CacheKey(domain=self.domain, tenant_id=tenant_id, discriminator=session_id)


@dataclass(frozen=True, slots=True)
class PromptCacheDomain(_DomainBase):
    """Rendered prompts, keyed by name and version.

    Version is in the key, so a deployment change cannot serve a stale prompt: the new version
    is simply a different key. The TTL only bounds memory.
    """

    @property
    def domain(self) -> CacheDomain:
        return CacheDomain.PROMPT

    def key(self, tenant_id: uuid.UUID, *, name: str, version: str) -> CacheKey:
        return CacheKey(domain=self.domain, tenant_id=tenant_id, discriminator=f"{name}@{version}")


@dataclass(frozen=True, slots=True)
class GraphCacheDomain(_DomainBase):
    """Graph traversals, keyed by entry entity and hop budget.

    Hop budget is in the key because a two-hop traversal is a superset of a one-hop one, and
    serving the wider result for the narrower request would silently widen every downstream
    inference.
    """

    @property
    def domain(self) -> CacheDomain:
        return CacheDomain.GRAPH

    def key(self, tenant_id: uuid.UUID, *, entity: str, max_hops: int) -> CacheKey:
        return CacheKey.for_content(self.domain, tenant_id, entity.casefold(), max_hops)


@dataclass(frozen=True, slots=True)
class CacheDomains:
    """Every domain, constructed together so TTLs come from one policy."""

    embedding: EmbeddingCacheDomain
    retrieval: RetrievalCacheDomain
    session: SessionCacheDomain
    prompt: PromptCacheDomain
    graph: GraphCacheDomain

    async def invalidate_tenant_writes(self, tenant_id: uuid.UUID) -> dict[str, int]:
        """Drop what a write for this tenant makes stale.

        Retrieval and graph only. Embeddings are content-addressed so a new document adds keys
        rather than invalidating them, prompts are versioned, and sessions are the user's own
        state — sweeping any of those on ingest would discard work for no correctness gain.
        """
        return {
            "retrieval": await self.retrieval.invalidate_for_tenant(tenant_id),
            "graph": await self.graph.invalidate_for_tenant(tenant_id),
        }


def build_domains(cache: Cache, policy: CachePolicy) -> CacheDomains:
    """Construct all five domains from one backend and one policy."""
    return CacheDomains(
        embedding=EmbeddingCacheDomain(cache=cache, ttl_seconds=policy.embedding_ttl_seconds),
        retrieval=RetrievalCacheDomain(cache=cache, ttl_seconds=policy.retrieval_ttl_seconds),
        session=SessionCacheDomain(cache=cache, ttl_seconds=policy.session_ttl_seconds),
        prompt=PromptCacheDomain(cache=cache, ttl_seconds=policy.prompt_ttl_seconds),
        graph=GraphCacheDomain(cache=cache, ttl_seconds=policy.graph_ttl_seconds),
    )


class CachedEmbeddingProvider:
    """Satisfies Phase 2's ``EmbeddingCache`` protocol over a distributed cache.

    This is the whole integration story for embeddings: the retrieval service already takes an
    ``EmbeddingCache``, so swapping its in-process LRU for a shared Redis cache is a
    constructor argument in the composition root and no change at all to Phase 2.

    Phase 2's protocol has no tenant in its signature — it keys on content and model, which is
    correct for a per-process cache. A shared cache needs one, so the tenant is bound here at
    construction. One provider instance therefore serves one tenant, which is what the gateway
    builds per request.
    """

    def __init__(self, domain: EmbeddingCacheDomain, *, tenant_id: uuid.UUID) -> None:
        self._domain = domain
        self._tenant_id = tenant_id

    async def get(self, key: str) -> tuple[float, ...] | None:
        # Phase 2's key is already `model_key:kind:sha256`, so it is passed through whole
        # rather than re-parsed: re-deriving it here would duplicate a format that lives in
        # another service and would break the moment that service changed it.
        return await self._domain.get_vector(self._tenant_id, model_key="", content_key=key)

    async def set(self, key: str, vector: tuple[float, ...]) -> None:
        await self._domain.set_vector(self._tenant_id, model_key="", content_key=key, vector=vector)
