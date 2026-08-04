"""Embedding orchestration: batching, retry, caching, and model versioning.

Providers do one thing — turn a list of texts into a list of vectors. Everything that makes
that production-viable lives here, once, so a new provider is a thin adapter rather than a
reimplementation of the same machinery.

Three behaviours are worth reading closely.

**Caching is content-addressed, model-scoped, and input-kind-scoped.** The key is
``model_key + input_kind + sha256(text)``. Chunk *ids* would be the obvious key and would be
wrong:
re-chunking produces new ids for identical text, and the whole point of the cache is that
re-running the pipeline after a chunking change does not re-pay for embeddings of text that
did not change. Including the model key means a model upgrade correctly misses the cache
rather than serving vectors from the wrong model.

**Retry only retries what is retryable.** :class:`EmbeddingError` carries that
distinction; retrying a permanent failure burns the budget and delays the real error.

**Order is preserved across cache hits and misses.** Callers zip results back onto their
inputs positionally, so a reordering bug here silently attaches every vector to the wrong
chunk — a failure that produces no error and corrupts an entire index.
"""

from __future__ import annotations

import asyncio
import hashlib
import random
from dataclasses import dataclass

from cip_core.logging import get_logger
from cip_retrieval.embeddings.base import (
    EmbeddingBatch,
    EmbeddingError,
    EmbeddingProvider,
    EmbeddingVector,
    InputKind,
)
from cip_retrieval.embeddings.cache import EmbeddingCache, NullEmbeddingCache

__all__ = ["EmbeddingService", "EmbeddingServiceOptions"]

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class EmbeddingServiceOptions:
    """Batching and retry policy."""

    batch_size: int = 64
    """Texts per provider call. Large enough to amortise request overhead, small enough
    that one failed batch does not force re-embedding thousands of texts."""

    max_concurrent_batches: int = 4
    """Concurrent provider calls. Bounded because an unbounded fan-out is the fastest way
    to hit a provider rate limit, which converts a fast ingest into a slow one."""

    max_attempts: int = 3
    base_backoff_seconds: float = 0.25
    max_backoff_seconds: float = 8.0
    jitter: bool = True
    """Randomises backoff. Without it, a batch of workers that fail together retry
    together, reproducing the overload that caused the failure."""

    def __post_init__(self) -> None:
        if self.batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        if self.max_concurrent_batches < 1:
            raise ValueError("max_concurrent_batches must be >= 1")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")


def content_key(text: str, model_key: str, kind: InputKind = InputKind.PASSAGE) -> str:
    """Cache key for a text under a specific model and input kind.

    ``kind`` is part of the key because asymmetric embedding models encode a query and a
    passage into *different* vectors for the same string — which is the entire reason
    :class:`InputKind` exists. Keying on content alone let a passage embedding be served for
    a later identical query (and vice versa), silently substituting the wrong encoding and
    degrading recall in a way no error surfaces.
    """
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return f"{model_key}:{kind.value}:{digest}"


class EmbeddingService:
    """Embeds text through a provider, with batching, retry, and caching."""

    def __init__(
        self,
        provider: EmbeddingProvider,
        *,
        cache: EmbeddingCache | None = None,
        options: EmbeddingServiceOptions | None = None,
    ) -> None:
        self._provider = provider
        self._cache = cache or NullEmbeddingCache()
        self._options = options or EmbeddingServiceOptions()

    @property
    def model_key(self) -> str:
        """Identity persisted with every vector this service produces."""
        return self._provider.info.key

    @property
    def dimensions(self) -> int:
        return self._provider.info.dimensions

    async def embed_texts(
        self, texts: list[str], *, kind: InputKind = InputKind.PASSAGE
    ) -> EmbeddingBatch:
        """Embed ``texts``, returning vectors in input order."""
        if not texts:
            return EmbeddingBatch(vectors=(), model=self._provider.info)

        model_key = self.model_key
        results: list[tuple[float, ...] | None] = [None] * len(texts)

        cache_hits = 0
        pending: list[int] = []
        for index, text in enumerate(texts):
            cached = await self._cache.get(content_key(text, model_key, kind))
            if cached is not None:
                results[index] = cached
                cache_hits += 1
            else:
                pending.append(index)

        provider_calls = 0
        if pending:
            # Deduplicate within the request: a document repeating a boilerplate line in
            # several chunks would otherwise pay for the same embedding several times.
            unique: dict[str, list[int]] = {}
            for index in pending:
                unique.setdefault(texts[index], []).append(index)

            unique_texts = list(unique)
            batches = [
                unique_texts[start : start + self._options.batch_size]
                for start in range(0, len(unique_texts), self._options.batch_size)
            ]
            semaphore = asyncio.Semaphore(self._options.max_concurrent_batches)

            async def _run(batch: list[str]) -> list[tuple[float, ...]]:
                async with semaphore:
                    return await self._embed_with_retry(batch, kind=kind)

            batch_results = await asyncio.gather(*(_run(batch) for batch in batches))
            provider_calls = len(batches)

            for batch, vectors in zip(batches, batch_results, strict=True):
                if len(vectors) != len(batch):
                    # A provider returning a different count than requested means every
                    # subsequent vector is attached to the wrong text. Fail loudly rather
                    # than write a silently corrupted index.
                    raise EmbeddingError(
                        f"Provider returned {len(vectors)} vectors for {len(batch)} inputs"
                    )
                for text, vector in zip(batch, vectors, strict=True):
                    await self._cache.set(content_key(text, model_key, kind), vector)
                    for index in unique[text]:
                        results[index] = vector

        missing = [index for index, value in enumerate(results) if value is None]
        if missing:  # pragma: no cover - indicates a provider contract violation
            raise EmbeddingError(f"No embedding produced for {len(missing)} input(s)")

        _log.debug(
            "embeddings.generated",
            count=len(texts),
            cache_hits=cache_hits,
            provider_calls=provider_calls,
            model=model_key,
        )
        # No `if value is not None` filter here: `missing` above has already established
        # that there are none, and a filter would silently *shorten* the tuple if that ever
        # stopped holding — turning a loud failure into every caller zipping vectors onto
        # the wrong texts.
        complete: list[tuple[float, ...]] = [value for value in results if value is not None]
        return EmbeddingBatch(
            vectors=tuple(EmbeddingVector(values=value, model_key=model_key) for value in complete),
            model=self._provider.info,
            cache_hits=cache_hits,
            provider_calls=provider_calls,
        )

    async def embed_query(self, text: str) -> EmbeddingVector:
        """Embed a search query.

        Separate from :meth:`embed_texts` because asymmetric models encode queries and
        passages differently, and using the passage encoding for queries degrades recall.
        """
        batch = await self.embed_texts([text], kind=InputKind.QUERY)
        return batch.vectors[0]

    async def _embed_with_retry(
        self, batch: list[str], *, kind: InputKind
    ) -> list[tuple[float, ...]]:
        """Call the provider, retrying transient failures with exponential backoff."""
        last_error: Exception | None = None

        for attempt in range(1, self._options.max_attempts + 1):
            try:
                return await self._provider.embed(batch, kind=kind)
            except EmbeddingError as exc:
                last_error = exc
                if not exc.retryable or attempt == self._options.max_attempts:
                    raise
            except Exception as exc:
                # An unrecognised exception is treated as retryable: most provider SDK
                # failures are transport-level and transient, and the attempt cap bounds
                # the cost of being wrong.
                last_error = exc
                if attempt == self._options.max_attempts:
                    raise EmbeddingError(
                        f"Embedding failed after {attempt} attempt(s): {type(exc).__name__}"
                    ) from exc

            delay = min(
                self._options.base_backoff_seconds * (2 ** (attempt - 1)),
                self._options.max_backoff_seconds,
            )
            if self._options.jitter:
                delay *= 0.5 + random.random()
            _log.warning(
                "embeddings.retry",
                attempt=attempt,
                max_attempts=self._options.max_attempts,
                delay_seconds=round(delay, 3),
                error=type(last_error).__name__,
            )
            await asyncio.sleep(delay)

        raise EmbeddingError(  # pragma: no cover - loop always returns or raises
            f"Embedding failed: {last_error}"
        )
