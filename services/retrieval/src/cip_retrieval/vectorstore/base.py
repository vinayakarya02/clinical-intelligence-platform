"""Vector store contract.

Two rules are enforced by the type system rather than by convention, because both are
tenant-isolation failures if violated:

**Every query carries a tenant.** :class:`VectorQuery` requires ``tenant_id``; there is no
way to construct an unscoped query. ADR-0003's "no code path exempted" rule is only
checkable if the unscoped path does not exist.

**Filtering happens inside the search, not after it.** Post-filtering an approximate-nearest-
neighbour top-K is not equivalent to filtering the candidate set: if another tenant's
documents crowd out this tenant's from the top-K, post-filtering returns *nothing* while
pre-filtering returns the correct results. This is the Phase 0 review's finding D5, and it
is why every implementation must push filters down rather than filter the returned list.

Every stored vector records the ``model_key`` that produced it. Embeddings from two models
are not comparable, and mixing them silently returns nonsense rankings rather than failing,
so queries always filter on the model that produced the query vector.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

__all__ = ["VectorMatch", "VectorQuery", "VectorRecord", "VectorStore"]


@dataclass(frozen=True, slots=True)
class VectorRecord:
    """A vector plus the metadata needed to filter and cite it."""

    id: str
    """Chunk id. Stable across re-indexing so an upsert replaces rather than duplicates."""

    tenant_id: uuid.UUID
    values: tuple[float, ...]
    model_key: str
    text: str

    document_id: uuid.UUID | None = None
    patient_id: uuid.UUID | None = None
    chunk_index: int | None = None
    section_name: str | None = None
    section_heading: str | None = None
    page_start: int | None = None
    page_end: int | None = None
    document_type: str | None = None
    source_system: str | None = None
    effective_date: str | None = None
    """ISO date string. Stored as text because the store is schemaless and comparisons are
    done on the retrieval side, where the type is known."""

    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.values:
            raise ValueError("VectorRecord.values must not be empty")
        if not self.model_key:
            raise ValueError("VectorRecord.model_key is required — vectors are model-specific")


@dataclass(frozen=True, slots=True)
class VectorQuery:
    """A similarity search, always tenant-scoped."""

    values: tuple[float, ...]
    tenant_id: uuid.UUID
    model_key: str
    top_k: int = 10

    patient_id: uuid.UUID | None = None
    document_types: tuple[str, ...] = ()
    section_names: tuple[str, ...] = ()
    document_ids: tuple[uuid.UUID, ...] = ()
    min_score: float | None = None
    """Similarity floor in [0, 1]. Applied after scoring — a threshold cannot be pushed
    into an ANN index — but before the result is returned, so callers never see matches
    they would have discarded."""

    num_candidates: int | None = None
    """ANN candidate pool size. Larger trades latency for recall; defaults to a multiple of
    ``top_k`` in implementations that need it."""

    def __post_init__(self) -> None:
        if not self.values:
            raise ValueError("VectorQuery.values must not be empty")
        if self.top_k < 1:
            raise ValueError("top_k must be >= 1")
        if self.min_score is not None and not 0.0 <= self.min_score <= 1.0:
            raise ValueError("min_score must be in [0, 1]")


@dataclass(frozen=True, slots=True)
class VectorMatch:
    """A search hit with its similarity score."""

    record: VectorRecord
    score: float
    """Cosine similarity mapped to [0, 1], where 1 is identical. Normalised across
    implementations so a threshold means the same thing regardless of backend."""


@runtime_checkable
class VectorStore(Protocol):
    """Stores and searches embedding vectors."""

    @property
    def name(self) -> str: ...

    async def upsert(self, records: list[VectorRecord]) -> int:
        """Insert or replace records by id. Returns the number written."""
        ...

    async def search(self, query: VectorQuery) -> list[VectorMatch]:
        """Return the closest matches, filtered and ordered by descending score."""
        ...

    async def delete_document(self, document_id: uuid.UUID, *, tenant_id: uuid.UUID) -> int:
        """Delete every vector for a document. Returns the number removed."""
        ...

    async def count(self, *, tenant_id: uuid.UUID) -> int:
        """Number of vectors held for a tenant."""
        ...

    async def health_check(self) -> dict[str, Any]: ...
