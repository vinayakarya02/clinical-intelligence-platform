"""In-memory exact vector search.

Not a mock. It implements the full :class:`VectorStore` contract with identical filter and
threshold semantics, so the retrieval pipeline behaves the same way against it as against
Atlas. That matters because Atlas Vector Search is an Atlas-only feature: without a local
implementation there is no way to run retrieval on a developer machine or in CI, and every
retrieval bug would surface only in a cloud environment (ADR-0007).

It is exact rather than approximate, which makes it *more* accurate than Atlas, not less —
useful for tests, because a recall assertion that fails here is a real bug rather than ANN
noise. The cost is O(n) per query, so it is viable for development corpora and small
tenants and nothing more. ``Settings`` refuses it in deployed environments.
"""

from __future__ import annotations

import math
import uuid
from typing import Any

from cip_core.logging import get_logger
from cip_retrieval.vectorstore.base import VectorMatch, VectorQuery, VectorRecord

__all__ = ["InMemoryVectorStore", "cosine_similarity"]

_log = get_logger(__name__)


def cosine_similarity(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    """Cosine similarity of two equal-length vectors, in [-1, 1].

    Raises on a dimension mismatch rather than truncating: two vectors of different widths
    come from different models, and silently comparing them produces a meaningless score
    that looks like a valid one.
    """
    if len(a) != len(b):
        raise ValueError(f"Dimension mismatch: {len(a)} != {len(b)}")
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def to_unit_interval(similarity: float) -> float:
    """Map cosine [-1, 1] onto [0, 1].

    Normalised so ``min_score`` means the same thing across backends: Atlas already returns
    a [0, 1] score, and a threshold that silently meant something different per backend
    would be a configuration trap.
    """
    return max(0.0, min(1.0, (similarity + 1.0) / 2.0))


class InMemoryVectorStore:
    """Exact brute-force vector search over an in-process dictionary."""

    def __init__(self) -> None:
        self._records: dict[str, VectorRecord] = {}

    @property
    def name(self) -> str:
        return "memory"

    async def upsert(self, records: list[VectorRecord]) -> int:
        for record in records:
            self._records[record.id] = record
        return len(records)

    async def search(self, query: VectorQuery) -> list[VectorMatch]:
        """Filter first, then score — the same ordering Atlas enforces in-index."""
        matches: list[VectorMatch] = []

        for record in self._records.values():
            if not self._passes_filters(record, query):
                continue
            score = to_unit_interval(cosine_similarity(query.values, record.values))
            if query.min_score is not None and score < query.min_score:
                continue
            matches.append(VectorMatch(record=record, score=score))

        matches.sort(key=lambda match: match.score, reverse=True)
        return matches[: query.top_k]

    @staticmethod
    def _passes_filters(record: VectorRecord, query: VectorQuery) -> bool:
        """Every filter that Atlas applies inside the index, applied before scoring."""
        if record.tenant_id != query.tenant_id:
            return False
        if record.model_key != query.model_key:
            # Vectors from another model are not comparable; including them would produce
            # a plausible-looking score from an incompatible space.
            return False
        if query.patient_id is not None and record.patient_id != query.patient_id:
            return False
        if query.document_types and record.document_type not in query.document_types:
            return False
        if query.section_names and record.section_name not in query.section_names:
            return False
        return not (query.document_ids and record.document_id not in query.document_ids)

    async def delete_document(self, document_id: uuid.UUID, *, tenant_id: uuid.UUID) -> int:
        doomed = [
            key
            for key, record in self._records.items()
            if record.document_id == document_id and record.tenant_id == tenant_id
        ]
        for key in doomed:
            del self._records[key]
        return len(doomed)

    async def count(self, *, tenant_id: uuid.UUID) -> int:
        return sum(1 for record in self._records.values() if record.tenant_id == tenant_id)

    async def health_check(self) -> dict[str, Any]:
        return {"status": "ok", "backend": self.name, "vectors": len(self._records)}

    def clear(self) -> None:
        """Drop all vectors. Test helper; not part of the protocol."""
        self._records.clear()
