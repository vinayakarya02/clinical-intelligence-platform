"""Clinical safety detectors.

Five detectors, each producing typed findings rather than a boolean. A boolean gate would
force every concern into "answer" or "refuse", when the clinically useful middle — answer,
but say what is uncertain — is where most findings belong.

Severity decides the handling. ``BLOCK`` suppresses the answer entirely; everything below is
attached to it, because a clinician reading a caveat is better served than one reading
nothing. Only two conditions block: no evidence at all, and a dangerous medication
combination that the answer does not already surface.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from cip_copilot.domain import Claim, Evidence, EvidenceKind
from cip_copilot.textutil import MEASUREMENT
from cip_core.logging import get_logger

__all__ = [
    "SafetyFinding",
    "SafetyReport",
    "Severity",
    "assess_safety",
]

_log = get_logger(__name__)


class Severity(StrEnum):
    """How a finding is handled."""

    INFO = "info"
    CAUTION = "caution"
    WARNING = "warning"
    BLOCK = "block"

    @property
    def rank(self) -> int:
        return {"info": 0, "caution": 1, "warning": 2, "block": 3}[self.value]


@dataclass(frozen=True, slots=True)
class SafetyFinding:
    """One detected concern."""

    code: str
    severity: Severity
    message: str
    detail: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": str(self.severity),
            "message": self.message,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class SafetyReport:
    """Everything the detectors found."""

    findings: tuple[SafetyFinding, ...] = ()

    @property
    def blocks(self) -> bool:
        return any(f.severity is Severity.BLOCK for f in self.findings)

    @property
    def highest(self) -> Severity:
        return max((f.severity for f in self.findings), key=lambda s: s.rank, default=Severity.INFO)

    def blocking(self) -> tuple[SafetyFinding, ...]:
        return tuple(f for f in self.findings if f.severity is Severity.BLOCK)

    def messages(self) -> tuple[str, ...]:
        return tuple(f.message for f in self.findings)


#: Terms whose clinical meaning depends on context. Each maps to what it could mean, so the
#: caveat can name the ambiguity instead of vaguely reporting one.
_AMBIGUOUS_TERMS: dict[str, tuple[str, ...]] = {
    "ms": ("multiple sclerosis", "mitral stenosis", "morphine sulfate"),
    "pcp": ("primary care physician", "pneumocystis pneumonia", "phencyclidine"),
    "ra": ("rheumatoid arthritis", "right atrium", "room air"),
    "cva": ("cerebrovascular accident", "costovertebral angle"),
    "dc": ("discharge", "discontinue"),
    "bd": ("twice daily", "bipolar disorder"),
    "sob": ("shortness of breath", "side of bed"),
}

#: Questions where old evidence is materially misleading rather than merely dated.
_TIME_SENSITIVE = re.compile(
    r"\b(?:current|currently|now|today|latest|most recent|still|at present|active)\b", re.I
)

#: Beyond this, evidence answering a time-sensitive question gets a caution. Deliberately
#: generous — a year-old discharge summary is often the most recent record there is.
_STALE_AFTER_DAYS = 365


def assess_safety(
    *,
    question: str,
    evidence: tuple[Evidence, ...],
    claims: tuple[Claim, ...],
    answer_text: str = "",
    tool_data: dict[str, Any] | None = None,
    today: dt.date | None = None,
) -> SafetyReport:
    """Run every detector over the assembled answer."""
    reference = today or dt.date.today()
    findings: list[SafetyFinding] = []

    findings.extend(_insufficient_evidence(evidence, claims))
    findings.extend(_dangerous_combination(tool_data or {}, answer_text))
    findings.extend(_contradictions(evidence))
    findings.extend(_staleness(question, evidence, reference))
    findings.extend(_ambiguity(question))

    report = SafetyReport(findings=tuple(findings))
    if report.findings:
        _log.info(
            "safety.assessed",
            findings=len(report.findings),
            highest=str(report.highest),
            codes=[f.code for f in report.findings],
        )
    return report


def _insufficient_evidence(
    evidence: tuple[Evidence, ...], claims: tuple[Claim, ...]
) -> list[SafetyFinding]:
    """No evidence, or evidence that supported nothing."""
    if not evidence:
        return [
            SafetyFinding(
                code="no_evidence",
                severity=Severity.BLOCK,
                message="No evidence was retrieved for this question.",
            )
        ]
    if not claims:
        return [
            SafetyFinding(
                code="no_supported_claims",
                severity=Severity.BLOCK,
                message=(
                    "Evidence was retrieved but none of it supports an answer to this question."
                ),
                detail={"evidence_count": len(evidence)},
            )
        ]
    if len(claims) == 1 and claims[0].support.weight < 1.0:
        return [
            SafetyFinding(
                code="thin_evidence",
                severity=Severity.CAUTION,
                message="This rests on a single indirect piece of evidence.",
                detail={"support": str(claims[0].support)},
            )
        ]
    return []


def _dangerous_combination(tool_data: dict[str, Any], answer_text: str) -> list[SafetyFinding]:
    """A known interaction among the patient's medications.

    Blocks only when the answer does not already surface it. An answer that *is* the
    interaction warning must not be suppressed by the interaction warning — but an answer
    about something else, produced while an interaction sits in the retrieved data, must not
    go out silently either.
    """
    pairs = tool_data.get("interaction_pairs") or []
    if not pairs:
        return []

    described = answer_text.lower()
    unsurfaced = [
        pair
        for pair in pairs
        if not (
            _mentions(described, str(pair.get("left", "")))
            and _mentions(described, str(pair.get("right", "")))
        )
    ]

    if not unsurfaced:
        return [
            SafetyFinding(
                code="interaction_reported",
                severity=Severity.INFO,
                message="A medication interaction is present and is reported in the answer.",
                detail={"pairs": pairs},
            )
        ]

    return [
        SafetyFinding(
            code="dangerous_combination",
            severity=Severity.BLOCK,
            message=(
                "A known medication interaction is present in this patient's data but is not "
                "addressed by the answer."
            ),
            detail={"pairs": unsurfaced},
        )
    ]


def _mentions(text: str, key: str) -> bool:
    """Whether an answer names a drug, given graph keys look like ``rx:lisinopril``."""
    name = key.split(":")[-1].strip().lower()
    return bool(name) and name in text


def _contradictions(evidence: tuple[Evidence, ...]) -> list[SafetyFinding]:
    """Two independent sources asserting incompatible values for one measurement and date.

    Derived evidence is excluded. A lab-trend summary is *computed from* the observations it
    would be compared against, so it is not an independent assertion — and because it carries
    a series of values stamped with the latest date, comparing it against its own inputs
    reported a contradiction on every trend question.
    """
    readings: dict[tuple[str, str], dict[str, list[str]]] = {}
    for item in (e for e in evidence if e.kind is not EvidenceKind.TOOL_RESULT):
        stamp = item.effective_date.isoformat() if item.effective_date else "undated"
        for analyte, value in MEASUREMENT.findall(item.content):
            slot = readings.setdefault((analyte.lower(), stamp), {})
            slot.setdefault(value, []).append(item.id)

    findings: list[SafetyFinding] = []
    for (analyte, stamp), values in sorted(readings.items()):
        # Two *sources* must disagree. Several values inside one item is a series — a lab
        # trend summary carries both endpoints and would otherwise report itself as a
        # conflict on every trend question.
        distinct_sources = {src for ids in values.values() for src in ids}
        if len(values) > 1 and len(distinct_sources) > 1:
            findings.append(
                SafetyFinding(
                    code="conflicting_evidence",
                    severity=Severity.WARNING,
                    message=(
                        f"Sources disagree on {analyte}"
                        + (f" for {stamp}" if stamp != "undated" else "")
                        + f": {', '.join(sorted(values))}."
                    ),
                    detail={"analyte": analyte, "date": stamp, "values": values},
                )
            )
    return findings


def _staleness(
    question: str, evidence: tuple[Evidence, ...], today: dt.date
) -> list[SafetyFinding]:
    """Old evidence answering a question about the present."""
    if not _TIME_SENSITIVE.search(question):
        return []

    dated = [e for e in evidence if e.effective_date is not None]
    if not dated:
        return [
            SafetyFinding(
                code="undated_evidence",
                severity=Severity.CAUTION,
                message=(
                    "This question asks about the current state, but none of the evidence "
                    "carries a date."
                ),
            )
        ]

    newest = max(e.effective_date for e in dated)  # type: ignore[type-var]
    age = (today - newest).days
    if age > _STALE_AFTER_DAYS:
        return [
            SafetyFinding(
                code="stale_evidence",
                severity=Severity.WARNING,
                message=(
                    f"This question asks about the current state, but the most recent "
                    f"evidence is {age} days old ({newest.isoformat()})."
                ),
                detail={"newest": newest.isoformat(), "age_days": age},
            )
        ]
    return []


def _ambiguity(question: str) -> list[SafetyFinding]:
    """A term in the question that resolves to several distinct clinical concepts."""
    tokens = {t.lower() for t in re.findall(r"[A-Za-z]+", question)}
    findings: list[SafetyFinding] = []
    for term, meanings in sorted(_AMBIGUOUS_TERMS.items()):
        if term in tokens:
            findings.append(
                SafetyFinding(
                    code="ambiguous_term",
                    severity=Severity.CAUTION,
                    message=(
                        f"'{term.upper()}' is ambiguous here — it can mean {', '.join(meanings)}."
                    ),
                    detail={"term": term, "meanings": list(meanings)},
                )
            )
    return findings


def evidence_agreement(evidence: tuple[Evidence, ...]) -> float:
    """Fraction of evidence kinds represented, as a corroboration proxy in [0, 1].

    A fact present in the structured record, the narrative, and the graph is corroborated in a
    way three passages from one document are not. Counting *kinds* rather than items is what
    keeps chunk overlap from inflating this.
    """
    if not evidence:
        return 0.0
    kinds = {item.kind for item in evidence}
    return round(len(kinds) / len(EvidenceKind), 4)
