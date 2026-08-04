"""Dense vector retrieval."""

from __future__ import annotations

import datetime as dt

from cip_core.logging import get_logger
from cip_retrieval.domain import (
    RetrievalCandidate,
    RetrievalQuery,
    RetrievalStrategy,
    SourceKind,
)
from cip_retrieval.embeddings.service import EmbeddingService
from cip_retrieval.vectorstore.base import VectorQuery, VectorStore

__all__ = ["VectorRetriever"]

_log = get_logger(__name__)


class VectorRetriever:
    """Embeds the query and searches the vector store.

    The query is embedded with the *same* service that embedded the corpus, so the
    ``model_key`` filter naturally matches. That coupling is deliberate: a retriever
    configured with a different model than the index would return nothing (correct but
    baffling) or, without the filter, nonsense scores from an incompatible vector space.
    """

    def __init__(self, store: VectorStore, embeddings: EmbeddingService) -> None:
        self._store = store
        self._embeddings = embeddings

    @property
    def strategy(self) -> RetrievalStrategy:
        return RetrievalStrategy.VECTOR

    async def retrieve(self, query: RetrievalQuery, *, limit: int) -> list[RetrievalCandidate]:
        embedded = await self._embeddings.embed_query(query.text)

        matches = await self._store.search(
            VectorQuery(
                values=embedded.values,
                tenant_id=query.tenant_id,
                model_key=self._embeddings.model_key,
                top_k=limit,
                patient_id=query.patient_id,
                document_types=tuple(query.filters.get("document_types", ())),
                section_names=tuple(query.filters.get("section_names", ())),
                min_score=query.min_score,
            )
        )

        candidates: list[RetrievalCandidate] = []
        for rank, match in enumerate(matches, start=1):
            record = match.record
            candidate = RetrievalCandidate(
                id=record.id,
                text=record.text,
                source_kind=SourceKind.DOCUMENT_CHUNK,
                tenant_id=record.tenant_id,
                document_id=record.document_id,
                chunk_index=record.chunk_index,
                section_name=record.section_name,
                section_heading=record.section_heading,
                page_start=record.page_start,
                page_end=record.page_end,
                effective_date=_parse_date(record.effective_date),
                document_type=record.document_type,
                source_system=record.source_system,
                metadata=dict(record.metadata),
            )
            candidates.append(candidate.with_rank(RetrievalStrategy.VECTOR, rank, match.score))

        _log.debug("retrieval.vector", returned=len(candidates), limit=limit)
        return candidates


def _parse_date(value: str | None) -> dt.date | None:
    """Parse an ISO date, tolerating malformed values.

    A bad date must not fail retrieval: it costs a freshness signal during reranking, which
    is far cheaper than dropping a clinically relevant chunk.
    """
    if not value:
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        return None
