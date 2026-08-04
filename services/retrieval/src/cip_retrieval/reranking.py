"""Reranking.

Fusion orders by *how many strategies agreed and how highly*. That is a strong signal and a
blind one: it knows nothing about whether a chunk is a superseded note, a low-confidence
graph inference, or a lab report from 2019 answering a question about today. Reranking adds
those judgements.

Phase 0 specifies a cross-encoder reranker, which needs a model this phase does not have
(the embedding bake-off has not run). Rather than stub it, this ships
:class:`FeatureReranker` — a transparent linear scorer over signals the pipeline already
computes. It is genuinely useful now, it is the baseline a cross-encoder must beat, and it
has a property a cross-encoder does not: every score decomposes into named features, so
"why was this ranked first" is answerable. In a clinical setting that is worth something on
its own.

The :class:`Reranker` protocol is what a cross-encoder implements later. Two
implementations are not required to justify it — the seam is the point, and the feature
scores it produces feed the evaluation framework either way.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, replace
from typing import Protocol, runtime_checkable

from cip_core.logging import get_logger
from cip_retrieval.domain import RetrievalCandidate, RetrievalQuery, SourceKind

__all__ = ["FeatureReranker", "RerankWeights", "Reranker"]

_log = get_logger(__name__)

_TOKEN = re.compile(r"[a-z0-9]+(?:[./-][a-z0-9]+)*")

#: Document types ordered by evidentiary weight for clinical questions. A guideline or
#: trial protocol is authored and reviewed; an unclassified upload is neither.
_SOURCE_QUALITY: dict[str, float] = {
    "guideline": 1.0,
    "trial_protocol": 0.95,
    "discharge_summary": 0.9,
    "lab_report": 0.9,
    "radiology_note": 0.85,
    "adverse_event_report": 0.85,
    "literature": 0.8,
    "fhir_bundle": 0.75,
    "hl7v2_message": 0.75,
    "unknown": 0.5,
}

#: Half-life for the freshness decay. Clinical relevance decays slowly — a two-year-old
#: discharge summary is still highly relevant — so this is deliberately long. A short
#: half-life would bury a patient's only relevant record because it is old.
_FRESHNESS_HALF_LIFE_DAYS = 730.0


@runtime_checkable
class Reranker(Protocol):
    """Reorders fused candidates."""

    @property
    def name(self) -> str: ...

    async def rerank(
        self, query: RetrievalQuery, candidates: list[RetrievalCandidate], *, limit: int
    ) -> list[RetrievalCandidate]: ...


@dataclass(frozen=True, slots=True)
class RerankWeights:
    """Relative influence of each reranking feature.

    Defaults weight ``fusion`` highest because it aggregates the retrievers' own judgement,
    which is the strongest single signal available. The rest adjust rather than override —
    a freshness or source-quality signal should reorder near-ties, not promote an
    irrelevant chunk over a relevant one.
    """

    fusion: float = 3.0
    lexical_overlap: float = 1.5
    section_match: float = 1.0
    graph_support: float = 1.2
    clinical_confidence: float = 1.0
    source_quality: float = 0.8
    freshness: float = 0.5

    def as_dict(self) -> dict[str, float]:
        return {
            "fusion": self.fusion,
            "lexical_overlap": self.lexical_overlap,
            "section_match": self.section_match,
            "graph_support": self.graph_support,
            "clinical_confidence": self.clinical_confidence,
            "source_quality": self.source_quality,
            "freshness": self.freshness,
        }


#: Query markers that make a section especially relevant. Used by the ``section_match``
#: feature: a medication question should prefer the medication section over a passing
#: mention of a drug in the narrative.
#: Each alternation is wrapped in a group so the leading ``\b`` applies to every branch —
#: without the group it anchors only the first, and the rest match mid-word.
_SECTION_AFFINITY: tuple[tuple[re.Pattern[str], frozenset[str]], ...] = (
    (re.compile(r"\b(?:medication|drug|dose|prescri)", re.I), frozenset({"medications"})),
    (
        re.compile(r"\b(?:lab|sodium|potassium|creatinine|troponin|hemoglobin|glucose)", re.I),
        frozenset({"laboratory_results"}),
    ),
    (re.compile(r"\ballerg", re.I), frozenset({"allergies"})),
    (
        re.compile(r"\b(?:diagnos|condition|problem)", re.I),
        frozenset({"diagnosis", "problem_list", "past_medical_history"}),
    ),
    (
        re.compile(r"\b(?:imaging|x-?ray|ct|mri|ultrasound|radiograph|scan)\b", re.I),
        frozenset({"findings", "imaging"}),
    ),
    # Deliberately multi-word. A bare "admission" or "hospital" marker claimed the hospital
    # course for every "...on admission?" lab lookup, which is a temporal anchor rather
    # than a request for the narrative — and it outranked the lab table holding the value.
    (
        re.compile(r"\b(?:hospital course|clinical course|what happened|progress)\b", re.I),
        frozenset({"hospital_course", "assessment"}),
    ),
    (
        re.compile(r"\b(?:plan|discharge|follow)", re.I),
        frozenset({"plan", "disposition", "follow_up"}),
    ),
)


class FeatureReranker:
    """Linear scorer over interpretable relevance features.

    Every feature lands in :attr:`RetrievalCandidate.rerank_features`, so a ranking can be
    explained without re-running anything — which is what makes the ordering auditable and
    the weights tunable against the evaluation harness rather than by intuition.
    """

    def __init__(self, weights: RerankWeights | None = None, *, now: dt.date | None = None) -> None:
        self._weights = weights or RerankWeights()
        self._now = now
        """Injectable clock. Without it, freshness scores drift with the wall clock and
        every test asserting an ordering becomes time-dependent."""

    @property
    def name(self) -> str:
        return "feature-linear"

    async def rerank(
        self, query: RetrievalQuery, candidates: list[RetrievalCandidate], *, limit: int
    ) -> list[RetrievalCandidate]:
        """Score and reorder, best first."""
        if not candidates:
            return []

        today = self._now or dt.date.today()
        query_tokens = frozenset(_TOKEN.findall(query.text.lower()))
        preferred_sections = self._preferred_sections(query.text)

        # Fusion scores are unbounded and scale with strategy count, so normalise against
        # the best in this result set. Without it the fusion feature would dominate or
        # vanish depending on how many retrievers happened to fire.
        top_fusion = max((c.fused_score for c in candidates), default=0.0) or 1.0

        weights = self._weights.as_dict()
        reranked: list[RetrievalCandidate] = []

        for candidate in candidates:
            features = {
                "fusion": candidate.fused_score / top_fusion,
                "lexical_overlap": self._lexical_overlap(query_tokens, candidate.text),
                "section_match": self._section_match(candidate, preferred_sections),
                "graph_support": self._graph_support(candidate),
                "clinical_confidence": self._clinical_confidence(candidate),
                "source_quality": _SOURCE_QUALITY.get(candidate.document_type or "unknown", 0.6),
                "freshness": self._freshness(candidate.effective_date, today),
            }
            total_weight = sum(weights.values())
            score = sum(features[name] * weight for name, weight in weights.items()) / total_weight

            reranked.append(
                replace(
                    candidate,
                    rerank_score=round(score, 6),
                    rerank_features={k: round(v, 4) for k, v in features.items()},
                )
            )

        reranked.sort(key=lambda c: (-(c.rerank_score or 0.0), c.id))
        _log.debug("rerank.completed", reranker=self.name, scored=len(reranked), limit=limit)
        return reranked[:limit]

    @staticmethod
    def _preferred_sections(text: str) -> frozenset[str]:
        preferred: set[str] = set()
        for pattern, sections in _SECTION_AFFINITY:
            if pattern.search(text):
                preferred |= sections
        return frozenset(preferred)

    @staticmethod
    def _lexical_overlap(query_tokens: frozenset[str], text: str) -> float:
        """Fraction of query terms present in the candidate.

        Complements dense similarity rather than duplicating it: embeddings can rank a
        paraphrase highly while missing the exact drug name or lab code the question named,
        and this feature restores that signal.
        """
        if not query_tokens:
            return 0.0
        candidate_tokens = frozenset(_TOKEN.findall(text.lower()))
        return len(query_tokens & candidate_tokens) / len(query_tokens)

    @staticmethod
    def _section_match(candidate: RetrievalCandidate, preferred: frozenset[str]) -> float:
        if not preferred:
            return 0.5  # neutral: the query implies no particular section
        return 1.0 if (candidate.section_name or "") in preferred else 0.3

    @staticmethod
    def _graph_support(candidate: RetrievalCandidate) -> float:
        """Whether the knowledge graph corroborates this candidate.

        Nearer evidence counts for more: a direct edge is stronger corroboration than a
        three-hop chain, which may be a coincidental connection rather than a clinical one.
        """
        if not candidate.graph_evidence:
            return 0.0
        best = max(
            evidence.confidence / max(evidence.hops, 1) for evidence in candidate.graph_evidence
        )
        return min(1.0, best)

    @staticmethod
    def _clinical_confidence(candidate: RetrievalCandidate) -> float:
        """Confidence in the assertion itself.

        Document chunks are quotations of what a clinician wrote — taken at face value.
        Graph edges are *inferences*, so they carry the extraction's own confidence and are
        penalised when it is low.
        """
        if candidate.source_kind is SourceKind.DOCUMENT_CHUNK:
            return 0.9
        if not candidate.graph_evidence:
            return 0.5
        return min(1.0, max(e.confidence for e in candidate.graph_evidence))

    @staticmethod
    def _freshness(effective_date: dt.date | None, today: dt.date) -> float:
        """Exponential decay with a long half-life.

        An undated document scores neutral rather than zero: missing metadata is common and
        must not be mistaken for evidence of staleness.
        """
        if effective_date is None:
            return 0.5
        age_days = max(0, (today - effective_date).days)
        return 0.5 ** (age_days / _FRESHNESS_HALF_LIFE_DAYS)
