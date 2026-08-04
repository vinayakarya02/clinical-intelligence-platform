"""Keyword retriever adapter.

Wraps :class:`~cip_retrieval.retrievers.keyword.BM25Index` in the
:class:`~cip_retrieval.retrievers.base.Retriever` protocol. Kept separate from the index
itself so the ranking function stays independently testable and so an OpenSearch-backed
index can be substituted without touching retrieval code.

BM25 scores are unbounded, unlike the [0, 1] similarity a vector store returns. They are
normalised against the top hit here so the recorded score is comparable across strategies
for diagnostics and reranking features. This does **not** affect fusion: RRF consumes ranks,
not scores, precisely because cross-strategy score scales are not comparable.
"""

from __future__ import annotations

import datetime as dt

from cip_core.logging import get_logger
from cip_retrieval.domain import (
    RetrievalCandidate,
    RetrievalQuery,
    RetrievalStrategy,
    SourceKind,
)
from cip_retrieval.retrievers.keyword import BM25Index

__all__ = ["KeywordRetriever"]

_log = get_logger(__name__)


class KeywordRetriever:
    """Exact-term retrieval over a BM25 index."""

    def __init__(self, index: BM25Index) -> None:
        self._index = index

    @property
    def strategy(self) -> RetrievalStrategy:
        return RetrievalStrategy.KEYWORD

    async def retrieve(self, query: RetrievalQuery, *, limit: int) -> list[RetrievalCandidate]:
        results = self._index.search(
            query.text,
            tenant_id=query.tenant_id,
            top_k=limit,
            patient_id=query.patient_id,
            document_types=tuple(query.filters.get("document_types", ())),
            section_names=tuple(query.filters.get("section_names", ())),
        )
        if not results:
            return []

        top_score = results[0][1] or 1.0

        candidates: list[RetrievalCandidate] = []
        for rank, (document, score) in enumerate(results, start=1):
            candidate = RetrievalCandidate(
                id=document.id,
                text=document.text,
                source_kind=SourceKind.DOCUMENT_CHUNK,
                tenant_id=document.tenant_id,
                document_id=document.document_id,
                chunk_index=document.chunk_index,
                section_name=document.section_name,
                section_heading=document.section_heading,
                page_start=document.page_start,
                page_end=document.page_end,
                effective_date=_parse_date(document.effective_date),
                document_type=document.document_type,
                source_system=document.source_system,
                metadata=dict(document.metadata),
            )
            candidates.append(
                candidate.with_rank(RetrievalStrategy.KEYWORD, rank, score / top_score)
            )

        _log.debug("retrieval.keyword", returned=len(candidates), limit=limit)
        return candidates


def _parse_date(value: str | None) -> dt.date | None:
    if not value:
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        return None
