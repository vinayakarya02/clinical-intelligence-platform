"""Evidence aggregation and claim construction.

Two stages that are separated because they answer different questions. Aggregation asks *what
do we have* — one deduplicated, ranked evidence set from retrieval, the graph, and every tool.
Reasoning asks *what does it support* — a set of claims, each naming the evidence behind it.

Nothing downstream may assert anything that is not a claim, and a claim cannot be constructed
without evidence ids (:class:`~cip_copilot.domain.Claim`). That is the structural mechanism
behind "never produce unexplained conclusions": it is enforced by the type, not requested in a
prompt.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

from cip_copilot.domain import Claim, ClaimSupport, Evidence, EvidenceKind
from cip_copilot.textutil import extract_numbers
from cip_core.logging import get_logger

__all__ = [
    "AggregationResult",
    "aggregate_evidence",
    "build_claims",
    "corroborated_values",
    "evidence_recency",
    "extract_numbers",
]

_log = get_logger(__name__)

#: Tokens too common to indicate that a claim is supported by a passage.
_STOPWORDS = frozenset(
    {
        "the",
        "a",
        "an",
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
        "with",
        "as",
        "by",
        "this",
        "that",
        "from",
        "has",
        "have",
        "had",
        "no",
        "not",
        "patient",
        "recorded",
        "status",
    }
)

_TOKEN = re.compile(r"[a-z0-9]+(?:[./-][a-z0-9]+)*")

#: Evidence kinds ordered by how directly they support a clinical claim. A quoted passage a
#: clinician wrote outranks a graph inference; both outrank a derived computation.
_KIND_RANK: dict[EvidenceKind, int] = {
    EvidenceKind.DOCUMENT_CHUNK: 0,
    EvidenceKind.STRUCTURED_FACT: 1,
    EvidenceKind.TOOL_RESULT: 2,
    EvidenceKind.GRAPH_RELATIONSHIP: 3,
}


def _tokens(text: str) -> frozenset[str]:
    return frozenset(t for t in _TOKEN.findall(text.lower()) if t not in _STOPWORDS)


@dataclass(frozen=True, slots=True)
class AggregationResult:
    """One evidence set, and what it cost to build."""

    evidence: tuple[Evidence, ...]
    dropped_duplicates: int = 0
    dropped_over_budget: int = 0
    by_kind: dict[str, int] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.by_kind is None:
            counts: dict[str, int] = {}
            for item in self.evidence:
                counts[str(item.kind)] = counts.get(str(item.kind), 0) + 1
            object.__setattr__(self, "by_kind", counts)


def aggregate_evidence(
    groups: list[tuple[str, tuple[Evidence, ...]]], *, limit: int = 12
) -> AggregationResult:
    """Merge evidence from every source into one ranked, deduplicated set.

    Ranking is by kind, then producer confidence, then retrieval score. Kind leads because a
    passage a clinician wrote is better support than an inference about it, regardless of how
    confidently the inference was extracted — a high-confidence graph edge is still an edge.

    Deduplication is by normalised content, not id: the same lab value reaches the aggregator
    as a structured fact from a tool *and* as a sentence in a discharge summary, and packing
    both spends budget twice on one fact while making it look doubly corroborated.
    """
    seen: dict[str, Evidence] = {}
    dropped_duplicates = 0

    flat: list[Evidence] = []
    for _source, items in groups:
        flat.extend(items)

    ordered = sorted(
        flat,
        key=lambda e: (
            _KIND_RANK.get(e.kind, 9),
            -e.confidence,
            -(e.retrieval_score or 0.0),
            e.id,
        ),
    )

    for item in ordered:
        digest = " ".join(item.content.split()).casefold()
        if not digest:
            continue
        if digest in seen:
            dropped_duplicates += 1
            continue
        seen[digest] = item

    kept = list(seen.values())
    dropped_over_budget = max(0, len(kept) - limit)

    result = AggregationResult(
        evidence=tuple(kept[:limit]),
        dropped_duplicates=dropped_duplicates,
        dropped_over_budget=dropped_over_budget,
    )
    _log.debug(
        "reasoning.aggregated",
        kept=len(result.evidence),
        duplicates=dropped_duplicates,
        over_budget=dropped_over_budget,
    )
    return result


def build_claims(question: str, evidence: tuple[Evidence, ...]) -> tuple[Claim, ...]:
    """Turn an evidence set into claims relevant to the question.

    One claim per piece of evidence that is relevant, plus derived claims where several pieces
    agree. Relevance is token overlap with the question — deliberately simple, because the
    verification stage is what protects the answer, not this filter. A too-permissive filter
    here costs context; a too-permissive verifier costs correctness.

    Claims are *not* generated by a model. A model composes the prose from these claims later;
    what may be asserted is decided here, in code, from evidence.
    """
    question_tokens = _tokens(question)
    claims: list[Claim] = []

    for index, item in enumerate(evidence, start=1):
        content_tokens = _tokens(item.content)
        overlap = question_tokens & content_tokens

        # A structured fact or tool result was fetched *because* the plan asked for it, so it
        # is on-topic by construction. Only free-text passages have to earn their place.
        earned = item.kind is not EvidenceKind.DOCUMENT_CHUNK or bool(overlap)
        if not earned:
            continue

        support = _support_for(item, overlap, question_tokens)
        statement = _statement_for(item)
        claims.append(
            Claim(
                id=f"c{index}",
                statement=statement,
                evidence_ids=(item.id,),
                support=support,
                numeric_values=extract_numbers(statement),
            )
        )

    _log.debug("reasoning.claims_built", claims=len(claims))
    return tuple(claims)


def _support_for(
    item: Evidence, overlap: frozenset[str], question_tokens: frozenset[str]
) -> ClaimSupport:
    """Classify how directly this evidence supports a claim about the question."""
    if item.kind is EvidenceKind.GRAPH_RELATIONSHIP:
        # A graph edge is an inference. Even a confident one is weaker than a passage,
        # and presenting the two as equal is what the developer prompt forbids.
        return ClaimSupport.DERIVED if item.confidence >= 0.8 else ClaimSupport.WEAK
    if item.kind is EvidenceKind.TOOL_RESULT:
        return ClaimSupport.DERIVED
    if not question_tokens:
        return ClaimSupport.WEAK
    coverage = len(overlap) / len(question_tokens)
    if coverage >= 0.5:
        return ClaimSupport.DIRECT
    return ClaimSupport.DIRECT if coverage >= 0.25 else ClaimSupport.WEAK


def _statement_for(item: Evidence) -> str:
    """Render evidence as an assertable sentence.

    A passage is quoted; anything else is attributed to what produced it. That distinction is
    what stops a computed trend or a graph inference from reading like something a clinician
    wrote.
    """
    content = " ".join(item.content.split())
    if item.kind is EvidenceKind.DOCUMENT_CHUNK:
        return content
    if item.kind is EvidenceKind.GRAPH_RELATIONSHIP:
        return f"The knowledge graph records that {content}"
    if item.kind is EvidenceKind.TOOL_RESULT:
        return f"Computed from the record: {content}"
    return f"The record states: {content}"


def corroborated_values(
    claims: tuple[Claim, ...], evidence: tuple[Evidence, ...]
) -> dict[str, tuple[str, ...]]:
    """Values asserted by more than one *kind* of source.

    This used to be emitted as derived claims ("the value 5.4 is corroborated by two
    independent kinds of source"). That was a design error: such a statement is a fact about
    the evidence set, not about the patient, so its words appear in no source and the
    verifier — correctly — rejected every one. Eight of fifteen claims per turn were being
    generated purely to be thrown away, dragging the verification score down with them.

    Corroboration belongs where it is actually used: the ``agreement`` confidence component
    and the explanation. It is reported here, not asserted.
    """
    by_id = {item.id: item for item in evidence}
    groups: dict[str, set[str]] = {}
    for claim in claims:
        for number in claim.numeric_values:
            for eid in claim.evidence_ids:
                item = by_id.get(eid)
                if item is not None:
                    groups.setdefault(number, set()).add(str(item.kind))
    return {
        number: tuple(sorted(kinds)) for number, kinds in sorted(groups.items()) if len(kinds) > 1
    }


def evidence_recency(evidence: tuple[Evidence, ...], *, today: dt.date | None = None) -> float:
    """Fraction of dated evidence that is under a year old, in [0, 1].

    Undated evidence is excluded from the denominator rather than counted as stale. Much
    clinical reference material is legitimately undated, and penalising it would push the
    score down for reasons that say nothing about the patient's record.
    """
    reference = today or dt.date.today()
    dated = [e for e in evidence if e.effective_date is not None]
    if not dated:
        return 0.5
    fresh = sum(1 for e in dated if (reference - e.effective_date).days <= 365)  # type: ignore[operator]
    return round(fresh / len(dated), 4)
