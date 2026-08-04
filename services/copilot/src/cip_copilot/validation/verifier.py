"""Claim verification — the reflection pass.

Deterministic checks of each claim against the evidence it names, rather than a model
critiquing its own output (docs/design/adr-0010-verification-not-self-critique.md). The
"external tool" the CRITIC pattern calls for is the evidence set, which is already assembled
and already provenanced.

A failing claim is **dropped, never rewritten**. Rewriting is how a reflection loop becomes a
generator of plausible corrections that are themselves unverified; dropping is auditable, and
the confidence score falls to match — which is the honest signal.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from cip_copilot.domain import Claim, Evidence, EvidenceKind
from cip_copilot.textutil import MEASUREMENT, extract_numbers
from cip_core.logging import get_logger

__all__ = [
    "VerificationOutcome",
    "VerificationReport",
    "verify_answer_text",
    "verify_claims",
]

_log = get_logger(__name__)

_TOKEN = re.compile(r"[a-z0-9]+(?:[./-][a-z0-9]+)*")

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
        "it",
        "records",
        "recorded",
        "computed",
        "states",
        "knowledge",
        "graph",
    }
)

#: Fraction of a claim's content tokens that must appear in its cited evidence. Not 1.0:
#: statements are rendered with framing ("The record states: …") that the source does not
#: contain. High enough that a claim about something absent cannot pass.
_SUPPORT_THRESHOLD = 0.6

#: Citation markers. Stripped before numeric checking: "[1]" is a reference to evidence,
#: not a clinical value, and counting it as one made *every* cited answer fail numeric
#: fidelity — the system could not answer anything it could also cite.
_CITATION_MARKER = re.compile(r"\[\d+\]")


class VerificationOutcome(StrEnum):
    """Why a claim passed or failed."""

    SUPPORTED = "supported"
    UNRESOLVED_CITATION = "unresolved_citation"
    UNSUPPORTED_CONTENT = "unsupported_content"
    FABRICATED_NUMBER = "fabricated_number"
    CONTRADICTED = "contradicted"


@dataclass(frozen=True, slots=True)
class VerificationReport:
    """Outcome of verifying a claim set."""

    verified: tuple[Claim, ...] = ()
    rejected: tuple[tuple[Claim, VerificationOutcome, str], ...] = ()

    @property
    def pass_rate(self) -> float:
        total = len(self.verified) + len(self.rejected)
        return round(len(self.verified) / total, 4) if total else 0.0

    @property
    def hallucination_rate(self) -> float:
        """Fraction of claims rejected for asserting something the evidence does not support.

        Distinct from the overall failure rate: an unresolved citation is a plumbing bug,
        while unsupported content or an invented number is the failure clinicians care about.
        """
        total = len(self.verified) + len(self.rejected)
        if not total:
            return 0.0
        fabrications = sum(
            1
            for _claim, outcome, _note in self.rejected
            if outcome
            in (
                VerificationOutcome.UNSUPPORTED_CONTENT,
                VerificationOutcome.FABRICATED_NUMBER,
            )
        )
        return round(fabrications / total, 4)

    def rejection_notes(self) -> tuple[str, ...]:
        return tuple(f"{claim.id}: {note}" for claim, _outcome, note in self.rejected)


def verify_claims(claims: tuple[Claim, ...], evidence: tuple[Evidence, ...]) -> VerificationReport:
    """Check every claim against the evidence it cites."""
    by_id = {item.id: item for item in evidence}
    verified: list[Claim] = []
    rejected: list[tuple[Claim, VerificationOutcome, str]] = []

    for claim in claims:
        outcome, note = _verify_one(claim, by_id)
        if outcome is VerificationOutcome.SUPPORTED:
            verified.append(claim.with_verification(verified=True))
        else:
            rejected.append((claim.with_verification(verified=False, notes=(note,)), outcome, note))

    report = VerificationReport(verified=tuple(verified), rejected=tuple(rejected))
    _log.debug(
        "validation.verified",
        passed=len(report.verified),
        rejected=len(report.rejected),
        pass_rate=report.pass_rate,
    )
    return report


def _verify_one(claim: Claim, by_id: dict[str, Evidence]) -> tuple[VerificationOutcome, str]:
    """Run every check against one claim, returning the first failure."""
    cited = [by_id[eid] for eid in claim.evidence_ids if eid in by_id]
    if len(cited) != len(claim.evidence_ids):
        missing = sorted(set(claim.evidence_ids) - set(by_id))
        return (
            VerificationOutcome.UNRESOLVED_CITATION,
            f"cites evidence that is not in the evidence set: {', '.join(missing)}",
        )

    corpus = " ".join(item.content for item in cited)

    # Numeric fidelity is exact-match by design. "Potassium 5.4" and "potassium 5.6" are
    # similar strings and clinically different facts; a tolerance here would be a decision to
    # sometimes report the wrong lab value.
    source_numbers = set(extract_numbers(corpus))
    invented = [n for n in claim.numeric_values if n not in source_numbers]
    if invented:
        return (
            VerificationOutcome.FABRICATED_NUMBER,
            f"states {', '.join(invented)}, which appears in no cited evidence",
        )

    claim_tokens = _content_tokens(claim.statement)
    if claim_tokens:
        supported = claim_tokens & _content_tokens(corpus)
        coverage = len(supported) / len(claim_tokens)
        if coverage < _SUPPORT_THRESHOLD:
            return (
                VerificationOutcome.UNSUPPORTED_CONTENT,
                f"only {coverage:.0%} of its content appears in the cited evidence",
            )

    contradiction = _find_numeric_contradiction(cited)
    if contradiction is not None:
        return VerificationOutcome.CONTRADICTED, contradiction

    return VerificationOutcome.SUPPORTED, ""


def _content_tokens(text: str) -> frozenset[str]:
    return frozenset(t for t in _TOKEN.findall(text.lower()) if t not in _STOPWORDS)


def _independent(evidence: list[Evidence]) -> list[Evidence]:
    """Evidence that asserts a value in its own right.

    A ``TOOL_RESULT`` is a computation over other evidence in the same set, so treating it as
    a second opinion double-counts its inputs — and, for a trend summary, sets one item
    against the very observations it summarises.
    """
    return [item for item in evidence if item.kind is not EvidenceKind.TOOL_RESULT]


def _find_numeric_contradiction(cited: list[Evidence]) -> str | None:
    """Detect two cited sources asserting different values for the same analyte.

    Only compares evidence with the *same effective date*, or where both are undated. A
    potassium of 5.4 on Monday and 4.1 on Friday is a trend, not a contradiction, and
    flagging it would make the check useless through noise.

    Derived evidence is excluded. A lab-trend summary is *computed from* the observations it
    would be compared against, so it is not an independent assertion — and because it carries
    a series of values stamped with the latest date, comparing it against its own inputs
    reported a contradiction on every trend question.
    """
    # Value -> the evidence ids asserting it. A contradiction needs *two independent sources*
    # disagreeing; several values inside one item is a series, not a conflict.
    readings: dict[tuple[str, str], dict[str, set[str]]] = {}
    for item in _independent(cited):
        stamp = item.effective_date.isoformat() if item.effective_date else "undated"
        for analyte, value in MEASUREMENT.findall(item.content):
            slot = readings.setdefault((analyte.lower(), stamp), {})
            slot.setdefault(value, set()).add(item.id)

    for (analyte, stamp), by_value in sorted(readings.items()):
        values = {v for v, sources in by_value.items() if sources}
        distinct_sources = {src for sources in by_value.values() for src in sources}
        if len(values) > 1 and len(distinct_sources) > 1:
            when = "on the same date" if stamp != "undated" else "in undated sources"
            return f"cited sources disagree on {analyte} {when}: {', '.join(sorted(values))}"
    return None


def verify_answer_text(
    text: str, claims: tuple[Claim, ...], evidence: tuple[Evidence, ...]
) -> tuple[bool, tuple[str, ...]]:
    """Check the generated prose against the claims it was built from.

    The claims were verified against evidence; this checks that generation did not introduce
    anything new on the way to prose. Both checks are needed — a model handed sound claims can
    still add a number, and a model handed nothing can still be fluent.
    """
    problems: list[str] = []

    claim_numbers = {n for claim in claims for n in claim.numeric_values}
    evidence_numbers = {n for item in evidence for n in extract_numbers(item.content)}
    allowed = claim_numbers | evidence_numbers

    prose = _CITATION_MARKER.sub(" ", text)
    for number in extract_numbers(prose):
        if number not in allowed:
            problems.append(f"the answer states '{number}', which appears in no claim or evidence")

    if text.strip() and not claims:
        problems.append("the answer asserts something but no verified claim supports it")

    return not problems, tuple(problems)
