"""Embedding generation.

Layering, outermost to innermost:

``EmbeddingService`` — batching, retry, caching, model identity. All business logic.
``EmbeddingProvider`` — the model contract. One method.
``HashingEmbeddingProvider`` — a dependency-free local baseline.

Business logic depends on the protocol only, so adopting a hosted model is a new adapter
plus a configuration change. Note that any provider sending clinical text to an external API
is subject to the BAA gating in docs/architecture/06-security-compliance.md §6, which covers
embedding calls explicitly, not only generation.
"""

from cip_retrieval.embeddings.base import (
    EmbeddingBatch,
    EmbeddingError,
    EmbeddingModelInfo,
    EmbeddingProvider,
    EmbeddingVector,
    InputKind,
)
from cip_retrieval.embeddings.cache import (
    EmbeddingCache,
    InMemoryEmbeddingCache,
    NullEmbeddingCache,
)
from cip_retrieval.embeddings.local import HashingEmbeddingProvider
from cip_retrieval.embeddings.service import (
    EmbeddingService,
    EmbeddingServiceOptions,
    content_key,
)

__all__ = [
    "EmbeddingBatch",
    "EmbeddingCache",
    "EmbeddingError",
    "EmbeddingModelInfo",
    "EmbeddingProvider",
    "EmbeddingService",
    "EmbeddingServiceOptions",
    "EmbeddingVector",
    "HashingEmbeddingProvider",
    "InMemoryEmbeddingCache",
    "InputKind",
    "NullEmbeddingCache",
    "content_key",
]
