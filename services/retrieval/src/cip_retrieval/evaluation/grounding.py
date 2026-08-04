"""Grounding, citation, and answer-quality metrics.

These score a *generated answer* against the context it was given. Phase 2 has no
generation step, so nothing calls them yet — they exist now because they define the
contract the Phase 3 answer path must satisfy, and because the retrieval-side metrics they
pair with (context precision/recall) are meaningless without them.

The design decision worth stating: these are **lexical and structural**, not model-graded.
An LLM-as-judge is more sensitive but costs a model call per evaluation, is itself
non-deterministic, and cannot run in CI. These are deterministic, free, and run on every
build. They under-detect subtle unfaithfulness and over-detect paraphrase — so they are a
regression gate, not a quality ceiling. An LLM judge belongs alongside them, scored on the
same eval set, not instead of them.

The exception is :func:`numeric_consistency`, which is *not* approximate. A dosage or lab
value either appears in the evidence or it does not, and that check is exact — which is why
Phase 0 (docs/architecture/04-conversational-ai.md §3) separates it from general grounding.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "GroundingReport",
    "answer_relevance",
    "assess_grounding",
    "citation_accuracy",
    "extract_citations",
    "numeric_consistency",
]

_CITATION = re.compile(r"\[(\d+)\]")
_TOKEN = re.compile(r"[a-z0-9]+(?:[./-][a-z0-9]+)*")
_NUMBER = re.compile(r"\b\d+(?:\.\d+)?\b")

_STOPWORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "and",
        "or",
        "of",
        "to",
        "in",
        "on",
        "for",
        "with",
        "was",
        "were",
        "is",
        "are",
        "be",
        "been",
        "at",
        "by",
        "from",
        "that",
        "this",
        "it",
        "as",
        "has",
        "had",
        "have",
        "no",
        "not",
        "patient",
    }
)


def extract_citations(answer: str) -> list[int]:
    """Citation indices referenced by an answer, in order of first appearance."""
    seen: list[int] = []
    for match in _CITATION.findall(answer):
        index = int(match)
        if index not in seen:
            seen.append(index)
    return seen


def citation_accuracy(answer: str, valid_indices: set[int]) -> float:
    """Fraction of the answer's citations that reference a real context block.

    Catches the specific failure where a model invents ``[7]`` for a context containing
    four blocks. Returns 1.0 for an answer with no citations — "did not cite" is measured
    by :func:`assess_grounding`, and conflating the two would let an uncited answer score
    as a citation failure or vice versa.
    """
    cited = extract_citations(answer)
    if not cited:
        return 1.0
    return sum(1 for index in cited if index in valid_indices) / len(cited)


def numeric_consistency(answer: str, context: str) -> float:
    """Fraction of numbers in the answer that appear verbatim in the context.

    Deliberately exact. "5 mg" reported as "50 mg" is a patient-safety incident, and no
    amount of semantic similarity makes it acceptable — which is why this check is
    string-level rather than embedding-based.

    Years and small integers are excluded: they appear in prose ("the second dose", "in
    2026") far more often than as clinical values, and counting them buries real numeric
    drift in noise.
    """
    answer_numbers = {
        value for value in _NUMBER.findall(answer) if not _is_incidental_number(value)
    }
    if not answer_numbers:
        return 1.0
    context_numbers = set(_NUMBER.findall(context))
    return sum(1 for value in answer_numbers if value in context_numbers) / len(answer_numbers)


def _is_incidental_number(value: str) -> bool:
    """Whether a number is prose rather than a clinical measurement."""
    if "." in value:
        return False  # decimals are almost always measurements
    as_int = int(value)
    return as_int <= 10 or 1900 <= as_int <= 2100


def groundedness(answer: str, context: str) -> float:
    """Fraction of the answer's content words that appear in the context.

    A blunt lexical proxy for "is this answer supported". It cannot detect a fluent
    paraphrase of an unsupported claim, and it penalises legitimate paraphrase — so it is
    useful as a *regression* signal (a sudden drop means something changed) rather than as
    an absolute quality measure.
    """
    answer_tokens = {t for t in _TOKEN.findall(answer.lower()) if t not in _STOPWORDS}
    if not answer_tokens:
        return 0.0
    context_tokens = {t for t in _TOKEN.findall(context.lower()) if t not in _STOPWORDS}
    return len(answer_tokens & context_tokens) / len(answer_tokens)


def answer_relevance(answer: str, question: str) -> float:
    """Overlap between the answer and the question's content words.

    Detects the failure where a model answers a different question than the one asked —
    common when retrieval surfaces adjacent-but-wrong evidence.
    """
    question_tokens = {t for t in _TOKEN.findall(question.lower()) if t not in _STOPWORDS}
    if not question_tokens:
        return 0.0
    answer_tokens = {t for t in _TOKEN.findall(answer.lower()) if t not in _STOPWORDS}
    return len(question_tokens & answer_tokens) / len(question_tokens)


@dataclass(frozen=True, slots=True)
class GroundingReport:
    """Combined grounding assessment for one answer."""

    citation_accuracy: float
    numeric_consistency: float
    groundedness: float
    answer_relevance: float
    citation_count: int
    uncited_claim_risk: bool
    """True when an answer makes substantive claims without citing anything — the shape of
    a confidently ungrounded response."""

    @property
    def passes(self) -> bool:
        """Whether the answer meets the minimum bar for release.

        Numeric consistency is an absolute gate, not a weighted contribution: a wrong
        dosage cannot be offset by good citations.
        """
        return (
            self.numeric_consistency >= 1.0
            and self.citation_accuracy >= 1.0
            and not self.uncited_claim_risk
        )

    def to_json(self) -> dict[str, float | int | bool]:
        return {
            "citation_accuracy": round(self.citation_accuracy, 4),
            "numeric_consistency": round(self.numeric_consistency, 4),
            "groundedness": round(self.groundedness, 4),
            "answer_relevance": round(self.answer_relevance, 4),
            "citation_count": self.citation_count,
            "uncited_claim_risk": self.uncited_claim_risk,
            "passes": self.passes,
        }


#: Answers longer than this without a citation are treated as substantive-but-uncited.
#: Short answers ("No allergies are documented.") legitimately need none.
_UNCITED_LENGTH_THRESHOLD = 200


def assess_grounding(
    *, answer: str, question: str, context: str, valid_citation_indices: set[int]
) -> GroundingReport:
    """Score an answer against its question and the context it was given."""
    citations = extract_citations(answer)
    return GroundingReport(
        citation_accuracy=citation_accuracy(answer, valid_citation_indices),
        numeric_consistency=numeric_consistency(answer, context),
        groundedness=groundedness(answer, context),
        answer_relevance=answer_relevance(answer, question),
        citation_count=len(citations),
        uncited_claim_risk=not citations and len(answer.strip()) > _UNCITED_LENGTH_THRESHOLD,
    )
