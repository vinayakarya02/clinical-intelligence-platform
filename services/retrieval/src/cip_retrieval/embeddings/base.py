"""Embedding provider contract.

The platform is model-agnostic by requirement, not by preference: the embedding model is
chosen by a bake-off (docs/architecture/02-rag-hybrid-retrieval.md §1.3), will be replaced
as better models ship, and differs between cloud and air-gapped deployments where no
external API call is permitted. Business logic therefore depends on this protocol and never
on a provider.

Every provider declares an :class:`EmbeddingModelInfo` carrying its identity and
dimensionality. That identity is persisted with each vector, because embeddings from two
models are not comparable: mixing them in one index silently returns nonsense rankings
rather than failing. Retrieval always filters on the model that produced the query vector.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

__all__ = [
    "EmbeddingBatch",
    "EmbeddingError",
    "EmbeddingModelInfo",
    "EmbeddingProvider",
    "EmbeddingVector",
    "InputKind",
]


class EmbeddingError(RuntimeError):
    """Raised when embedding generation fails irrecoverably.

    ``retryable`` distinguishes a transient provider fault (rate limit, timeout) from a
    permanent one (input too long, bad credentials). Retrying a permanent failure burns the
    retry budget and delays the real error; not retrying a transient one fails an ingest
    that would have succeeded a second later.
    """

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class InputKind(StrEnum):
    """Whether text is being embedded as a stored passage or as a search query.

    Asymmetric models (and instruction-tuned embedders generally) encode the two
    differently, and using the passage encoding for queries measurably degrades recall.
    Providers that make no distinction may ignore it.
    """

    PASSAGE = "passage"
    QUERY = "query"


@dataclass(frozen=True, slots=True)
class EmbeddingModelInfo:
    """Identity of the model that produced a vector."""

    provider: str
    model_name: str
    dimensions: int
    max_input_tokens: int = 8192
    supports_input_kind: bool = False

    @property
    def key(self) -> str:
        """Stable identifier persisted alongside every vector.

        Includes dimensionality so a provider silently changing output width — which does
        happen across model revisions — produces a different key rather than corrupting an
        existing index with incompatible vectors.
        """
        return f"{self.provider}/{self.model_name}/{self.dimensions}"


@dataclass(frozen=True, slots=True)
class EmbeddingVector:
    """A single embedding with the identity of the model that produced it."""

    values: tuple[float, ...]
    model_key: str

    def __post_init__(self) -> None:
        if not self.values:
            raise ValueError("Embedding vector must not be empty")

    @property
    def dimensions(self) -> int:
        return len(self.values)


@dataclass(frozen=True, slots=True)
class EmbeddingBatch:
    """Result of embedding several texts together.

    ``cache_hits`` is reported because cache effectiveness is the difference between a
    re-chunking run costing minutes and costing hours, and it is invisible without
    measurement.
    """

    vectors: tuple[EmbeddingVector, ...]
    model: EmbeddingModelInfo
    cache_hits: int = 0
    provider_calls: int = 0

    def __len__(self) -> int:
        return len(self.vectors)


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Turns text into vectors.

    Implementations handle only the provider call. Batching, retry, caching, and
    persistence belong to :class:`~cip_retrieval.embeddings.service.EmbeddingService`, so a
    new provider is a thin adapter rather than a reimplementation of that machinery.
    """

    @property
    def info(self) -> EmbeddingModelInfo: ...

    async def embed(
        self, texts: list[str], *, kind: InputKind = InputKind.PASSAGE
    ) -> list[tuple[float, ...]]:
        """Embed ``texts``, returning one vector per input in the same order.

        Raises :class:`EmbeddingError`. Implementations must preserve input order — callers
        zip results back onto their inputs positionally.
        """
        ...
