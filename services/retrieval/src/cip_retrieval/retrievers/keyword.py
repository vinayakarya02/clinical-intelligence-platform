"""Keyword retrieval (BM25).

Dense vectors are weak at exactly what clinical retrieval needs most often: exact matching
on drug names, ICD/SNOMED codes, and lab values. "Sodium 141" and "sodium 151" are
near-identical in embedding space and clinically different; BM25 separates them because it
matches terms, not meanings. This is why the Phase 0 design pairs dense and sparse retrieval
rather than treating vectors as sufficient (ADR-0001).

OpenSearch is the production backend. This in-process BM25 exists for the same reason the
in-memory vector store does — a keyword retriever that only runs against a cluster is one
that never gets tested — and it is a genuine implementation, not a stub: the ranking
function is standard Okapi BM25 with the usual parameters.

Clinical tokenisation matters here more than in general text. ``mg/dl``, ``0.04``, and
``covid-19`` must survive as single terms; splitting them destroys the exact-match property
that justifies having keyword search at all.
"""

from __future__ import annotations

import math
import re
import uuid
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

__all__ = ["BM25Index", "BM25Options", "IndexedDocument", "tokenize_clinical"]

_TOKEN_PATTERN = re.compile(r"[a-z0-9]+(?:[./-][a-z0-9]+)*")

#: Words carrying no discriminative power in clinical prose. Deliberately short: aggressive
#: stopword removal strips clinically meaningful negation ("no", "not", "without"), and
#: "no chest pain" versus "chest pain" is a clinical inversion, not a stylistic variant.
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "of",
        "to",
        "in",
        "on",
        "at",
        "for",
        "and",
        "or",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "this",
        "that",
        "these",
        "those",
        "with",
        "as",
        "by",
    }
)


def tokenize_clinical(text: str) -> list[str]:
    """Tokenise clinical text, preserving units, decimals, and hyphenated terms."""
    return [token for token in _TOKEN_PATTERN.findall(text.lower()) if token not in _STOPWORDS]


@dataclass(frozen=True, slots=True)
class BM25Options:
    """Okapi BM25 parameters."""

    k1: float = 1.5
    """Term-frequency saturation. 1.2-2.0 is the standard range."""

    b: float = 0.75
    """Length normalisation. Clinical documents vary enormously in length — a one-line lab
    result against a twenty-page discharge summary — so full normalisation matters more
    here than for uniform corpora."""

    def __post_init__(self) -> None:
        if self.k1 < 0:
            raise ValueError("k1 must be >= 0")
        if not 0.0 <= self.b <= 1.0:
            raise ValueError("b must be in [0, 1]")


@dataclass(frozen=True, slots=True)
class IndexedDocument:
    """A chunk in the keyword index."""

    id: str
    tenant_id: uuid.UUID
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
    metadata: dict[str, Any] = field(default_factory=dict)


class BM25Index:
    """In-process BM25 over tenant-scoped chunks.

    Statistics (document frequency, average length) are maintained **per tenant**. A shared
    IDF table would leak information across tenants — term rarity is a function of the
    corpus, so a global table lets one tenant's corpus influence another's ranking — and it
    would also be wrong, since relevance depends on the collection being searched.
    """

    def __init__(self, options: BM25Options | None = None) -> None:
        self._options = options or BM25Options()
        self._documents: dict[str, IndexedDocument] = {}
        self._terms: dict[str, Counter[str]] = {}
        self._lengths: dict[str, int] = {}
        self._by_tenant: dict[uuid.UUID, set[str]] = {}

    @property
    def name(self) -> str:
        return "bm25-memory"

    def add(self, documents: list[IndexedDocument]) -> int:
        """Index documents, replacing any with the same id."""
        for document in documents:
            previous = self._documents.get(document.id)
            if previous is not None and previous.tenant_id != document.tenant_id:
                # Re-indexing an id under a different tenant must not leave it in the old
                # tenant's set. The stale entry would still resolve through `_documents` to
                # the *new* document, handing the previous tenant another tenant's text.
                self._by_tenant.get(previous.tenant_id, set()).discard(document.id)

            tokens = tokenize_clinical(document.text)
            self._documents[document.id] = document
            self._terms[document.id] = Counter(tokens)
            self._lengths[document.id] = len(tokens)
            self._by_tenant.setdefault(document.tenant_id, set()).add(document.id)
        return len(documents)

    def remove_document(self, document_id: uuid.UUID, *, tenant_id: uuid.UUID) -> int:
        doomed = [
            key
            for key, doc in self._documents.items()
            if doc.document_id == document_id and doc.tenant_id == tenant_id
        ]
        for key in doomed:
            self._documents.pop(key, None)
            self._terms.pop(key, None)
            self._lengths.pop(key, None)
            self._by_tenant.get(tenant_id, set()).discard(key)
        return len(doomed)

    def count(self, *, tenant_id: uuid.UUID) -> int:
        return len(self._by_tenant.get(tenant_id, set()))

    def search(
        self,
        query: str,
        *,
        tenant_id: uuid.UUID,
        top_k: int = 10,
        patient_id: uuid.UUID | None = None,
        document_types: tuple[str, ...] = (),
        section_names: tuple[str, ...] = (),
    ) -> list[tuple[IndexedDocument, float]]:
        """Return the best-scoring documents, highest first.

        Filters are applied before scoring so corpus statistics reflect the searched
        collection, not the whole tenant.
        """
        candidate_ids = self._candidates(
            tenant_id=tenant_id,
            patient_id=patient_id,
            document_types=document_types,
            section_names=section_names,
        )
        if not candidate_ids:
            return []

        query_terms = tokenize_clinical(query)
        if not query_terms:
            return []

        total_documents = len(candidate_ids)
        average_length = (
            sum(self._lengths[doc_id] for doc_id in candidate_ids) / total_documents
        ) or 1.0

        document_frequency: Counter[str] = Counter()
        for term in set(query_terms):
            document_frequency[term] = sum(
                1 for doc_id in candidate_ids if self._terms[doc_id].get(term, 0) > 0
            )

        scored: list[tuple[IndexedDocument, float]] = []
        for doc_id in candidate_ids:
            score = self._score(
                doc_id,
                query_terms=query_terms,
                document_frequency=document_frequency,
                total_documents=total_documents,
                average_length=average_length,
            )
            if score > 0.0:
                scored.append((self._documents[doc_id], score))

        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[:top_k]

    def _candidates(
        self,
        *,
        tenant_id: uuid.UUID,
        patient_id: uuid.UUID | None,
        document_types: tuple[str, ...],
        section_names: tuple[str, ...],
    ) -> list[str]:
        candidates: list[str] = []
        for doc_id in self._by_tenant.get(tenant_id, set()):
            document = self._documents[doc_id]
            if patient_id is not None and document.patient_id != patient_id:
                continue
            if document_types and document.document_type not in document_types:
                continue
            if section_names and document.section_name not in section_names:
                continue
            candidates.append(doc_id)
        return candidates

    def _score(
        self,
        doc_id: str,
        *,
        query_terms: list[str],
        document_frequency: Counter[str],
        total_documents: int,
        average_length: float,
    ) -> float:
        """Okapi BM25 for one document."""
        terms = self._terms[doc_id]
        length = self._lengths[doc_id]
        k1, b = self._options.k1, self._options.b

        score = 0.0
        for term in query_terms:
            frequency = terms.get(term, 0)
            if frequency == 0:
                continue
            df = document_frequency[term]
            # The +0.5 smoothing is standard and matters at small corpus sizes: without it
            # a term appearing in every document produces a negative IDF and actively
            # penalises the documents that contain it.
            idf = math.log(1.0 + (total_documents - df + 0.5) / (df + 0.5))
            numerator = frequency * (k1 + 1.0)
            denominator = frequency + k1 * (1.0 - b + b * length / average_length)
            score += idf * numerator / denominator
        return score
