"""Domain types for clinical decision intelligence.

Imports nothing else from this package — the base of the dependency order, enforced by a test.

Two design points carry the phase.

:class:`Severity` and :class:`EvidenceQuality` are **separate enumerations**
(docs/design/adr-0020-severity-and-evidence.md). Every clinical reference that has survived in
practice separates *how bad if it happens* from *how sure are we that it does*, and collapsing
them produces either a suppressed contraindication or a shouted theoretical risk.

:class:`Recommendation` cannot be constructed without a citation and a provenance chain. That
is the structural form of "recommendations must always explain WHY" — it is a constructor
invariant, not a convention anybody can forget.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

__all__ = [
    "Citation",
    "ClinicalFact",
    "EvidenceQuality",
    "FactKind",
    "PatientContext",
    "ProvenanceLink",
    "Recommendation",
    "RecommendationKind",
    "ReviewState",
    "Severity",
]


class Severity(StrEnum):
    """How bad the consequence is if the concern is real.

    The four levels every major drug reference uses. ``CONTRAINDICATED`` is exempt from
    suppression everywhere in this codebase — it is the one level where the alert-fatigue
    trade does not apply, because the cost of missing it is unbounded.
    """

    CONTRAINDICATED = "contraindicated"
    MAJOR = "major"
    MODERATE = "moderate"
    MINOR = "minor"
    INFORMATIONAL = "informational"

    @property
    def rank(self) -> int:
        """Higher is more severe. Used for ordering and floor comparison."""
        return {
            "informational": 0,
            "minor": 1,
            "moderate": 2,
            "major": 3,
            "contraindicated": 4,
        }[self.value]

    @property
    def is_suppressible(self) -> bool:
        """Whether a suppression rule may ever hide this.

        Only contraindications are exempt. Everything else is subject to the volume controls
        that exist because published override rates run 49–96%
        (docs/design/adr-0021-alert-fatigue.md).
        """
        return self is not Severity.CONTRAINDICATED

    @property
    def cds_hooks_indicator(self) -> str:
        """The CDS Hooks card indicator this maps to.

        The specification allows only ``info``, ``warning``, and ``critical``, so four
        severities compress into three. ``major`` maps to ``critical`` rather than
        ``warning`` because a card that may be life-threatening should not render with the
        same weight as a moderate one.
        """
        return {
            "contraindicated": "critical",
            "major": "critical",
            "moderate": "warning",
            "minor": "info",
            "informational": "info",
        }[self.value]


class EvidenceQuality(StrEnum):
    """How well-documented the concern is.

    Independent of severity. Drives *wording*, never suppression: a contraindication with only
    theoretical documentation is still shown, and says so.
    """

    ESTABLISHED = "established"
    PROBABLE = "probable"
    SUSPECTED = "suspected"
    POSSIBLE = "possible"
    THEORETICAL = "theoretical"

    @property
    def weight(self) -> float:
        """Contribution to ranking. Never multiplied into severity."""
        return {
            "established": 1.0,
            "probable": 0.8,
            "suspected": 0.6,
            "possible": 0.4,
            "theoretical": 0.2,
        }[self.value]

    @property
    def qualifier(self) -> str:
        """How a recommendation citing this evidence should be worded.

        The point of separating the axes: the same severity reads differently depending on
        how sure the literature is, and a clinician needs that distinction to decide whether
        to act now or investigate.
        """
        return {
            "established": "is well documented",
            "probable": "is probable",
            "suspected": "is suspected but not well documented",
            "possible": "is possible; documentation is limited or conflicting",
            "theoretical": "is theoretical, based on mechanism rather than reports",
        }[self.value]


@dataclass(frozen=True, slots=True)
class Citation:
    """Where a clinical assertion came from.

    Mandatory on every knowledge artifact. An uncited clinical assertion cannot be reviewed
    or defended, so the loader refuses one (docs/design/adr-0019-knowledge-as-data.md).
    """

    source: str
    """The publishing body or document — "NICE NG136", "FDA label", "WHO EML"."""

    reference: str = ""
    url: str = ""
    published: dt.date | None = None
    note: str = ""

    def __post_init__(self) -> None:
        if not self.source.strip():
            raise ValueError("Citation.source must not be empty")

    def render(self) -> str:
        parts = [self.source]
        if self.reference:
            parts.append(self.reference)
        if self.published:
            parts.append(f"({self.published.isoformat()})")
        return " ".join(parts)


class FactKind(StrEnum):
    """What kind of clinical fact this is.

    A closed set, because rules match on it and a typo in an open-ended string is a rule that
    silently never fires.
    """

    CONDITION = "condition"
    MEDICATION = "medication"
    OBSERVATION = "observation"
    ALLERGY = "allergy"
    PROCEDURE = "procedure"
    ENCOUNTER = "encounter"
    DEMOGRAPHIC = "demographic"


@dataclass(frozen=True, slots=True)
class ClinicalFact:
    """One thing known about a patient, in the form rules evaluate against.

    A deliberately flat shape. Rules address facts by ``kind`` and ``code``/``name``, and a
    nested structure would push path navigation into the expression language — which is where
    a small safe language becomes a large unsafe one.
    """

    kind: FactKind
    name: str
    code: str | None = None
    code_system: str | None = None
    value: float | None = None
    unit: str | None = None
    effective: dt.date | None = None
    ended: dt.date | None = None
    status: str = "active"
    attributes: dict[str, Any] = field(default_factory=dict)
    source_ref: str = ""

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("ClinicalFact.name must not be empty")

    @property
    def is_active(self) -> bool:
        return self.status == "active" and self.ended is None

    def age_days(self, *, today: dt.date | None = None) -> int | None:
        """How old this fact is, or ``None`` if undated.

        ``None`` rather than a default is deliberate: a temporal rule must decide explicitly
        what to do about an undated fact, and treating undated as "today" would let a rule
        about recent labs fire on a lab of unknown age.
        """
        if self.effective is None:
            return None
        return ((today or dt.date.today()) - self.effective).days

    def matches(self, *, name: str | None = None, code: str | None = None) -> bool:
        """Whether this fact is the thing a rule is asking about.

        Name matching is case-insensitive substring, because clinical records spell the same
        drug several ways. Code matching is exact, because a code is either right or wrong.
        """
        if code is not None:
            return self.code == code
        if name is not None:
            return name.strip().lower() in self.name.lower()
        return False


@dataclass(frozen=True, slots=True)
class PatientContext:
    """Everything the engine knows about one patient at one moment.

    Passed whole to every stage. The engine never fetches — a caller assembles this, which is
    what keeps the decision path free of I/O and therefore deterministic and fast to test.
    """

    patient_id: uuid.UUID
    tenant_id: uuid.UUID
    facts: tuple[ClinicalFact, ...] = ()
    as_of: dt.date = field(default_factory=dt.date.today)
    age_years: int | None = None
    sex: str | None = None

    def of_kind(self, kind: FactKind, *, active_only: bool = False) -> tuple[ClinicalFact, ...]:
        found = tuple(f for f in self.facts if f.kind is kind)
        return tuple(f for f in found if f.is_active) if active_only else found

    def latest(self, kind: FactKind, name: str) -> ClinicalFact | None:
        """The most recent matching fact.

        Undated facts sort *below* dated ones rather than being excluded: a lab with no date
        is still evidence the value exists, and dropping it would make "no potassium recorded"
        indistinguishable from "potassium recorded without a date".
        """
        candidates = [f for f in self.of_kind(kind) if f.matches(name=name)]
        if not candidates:
            return None
        return max(candidates, key=lambda f: (f.effective is not None, f.effective or dt.date.min))

    def series(self, name: str) -> tuple[ClinicalFact, ...]:
        """Every observation of one analyte, oldest first."""
        found = [f for f in self.of_kind(FactKind.OBSERVATION) if f.matches(name=name)]
        return tuple(sorted(found, key=lambda f: f.effective or dt.date.min))

    def has(self, kind: FactKind, name: str, *, active_only: bool = True) -> bool:
        return any(f.matches(name=name) for f in self.of_kind(kind, active_only=active_only))


class RecommendationKind(StrEnum):
    """What sort of action is being proposed."""

    INVESTIGATION = "investigation"
    MEDICATION_CHANGE = "medication_change"
    MONITORING = "monitoring"
    REFERRAL = "referral"
    ALERT = "alert"
    INFORMATION = "information"


class ReviewState(StrEnum):
    """Where a recommendation sits in its approval lifecycle.

    No state transitions to ``ACCEPTED`` without an identified human. There is no auto-accept
    path anywhere in this codebase (docs/design/adr-0024-human-approval-gate.md).
    """

    PROPOSED = "proposed"
    UNDER_REVIEW = "under_review"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    EXPIRED = "expired"
    SUPPRESSED = "suppressed"

    @property
    def is_terminal(self) -> bool:
        return self in (
            ReviewState.ACCEPTED,
            ReviewState.REJECTED,
            ReviewState.EXPIRED,
            ReviewState.SUPPRESSED,
        )


@dataclass(frozen=True, slots=True)
class ProvenanceLink:
    """One step in the chain from knowledge to recommendation.

    Chained, these answer "how was this produced" as a readable path:
    guideline → evidence → rule → fact → recommendation.
    """

    kind: str
    identifier: str
    label: str
    detail: str = ""

    def render(self) -> str:
        return (
            f"{self.kind}:{self.identifier} ({self.label})"
            if self.label
            else f"{self.kind}:{self.identifier}"
        )


@dataclass(frozen=True, slots=True)
class Recommendation:
    """A proposed action, with everything needed to review it.

    Cannot be constructed without a citation and a provenance chain. That is the structural
    guarantee behind "recommendations must always explain WHY" — enforced by the type rather
    than requested in a review checklist.
    """

    id: str
    kind: RecommendationKind
    summary: str
    severity: Severity
    evidence_quality: EvidenceQuality
    citations: tuple[Citation, ...]
    provenance: tuple[ProvenanceLink, ...]

    detail: str = ""
    rationale: str = ""
    patient_id: uuid.UUID | None = None
    source_rule_ids: tuple[str, ...] = ()
    triggering_facts: tuple[str, ...] = ()
    state: ReviewState = ReviewState.PROPOSED
    suppression_reason: str = ""
    created_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))
    expires_at: dt.datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.summary.strip():
            raise ValueError("Recommendation.summary must not be empty")
        if len(self.summary) > 140:
            # The CDS Hooks card summary limit. Enforced at construction rather than at
            # render time so a recommendation that cannot be displayed cannot be created.
            raise ValueError(
                f"Recommendation.summary must be under 140 characters for a CDS Hooks card "
                f"(got {len(self.summary)})"
            )
        if not self.citations:
            raise ValueError(
                f"Recommendation '{self.id}' has no citation. An uncited clinical "
                "recommendation cannot be reviewed or defended."
            )
        if not self.provenance:
            raise ValueError(
                f"Recommendation '{self.id}' has no provenance chain, so it cannot explain "
                "how it was produced."
            )

    @property
    def priority(self) -> float:
        """Ordering score, derived at display time and never stored.

        Derived rather than stored so it cannot become the thing people reason about — the
        two axes are the real quantities (ADR-0020).
        """
        return round(self.severity.rank + self.evidence_quality.weight, 4)

    def explain(self) -> str:
        """Why this recommendation exists, in a form a clinician can check."""
        lines = [self.summary]
        if self.rationale:
            lines.append(f"Because: {self.rationale}")
        if self.triggering_facts:
            lines.append(f"Triggered by: {', '.join(self.triggering_facts)}")
        lines.append(
            f"Severity {self.severity.value}; the evidence {self.evidence_quality.qualifier}."
        )
        lines.append("Sources: " + "; ".join(c.render() for c in self.citations))
        if self.provenance:
            lines.append("Derivation: " + " → ".join(p.render() for p in self.provenance))
        return "\n".join(lines)

    def with_state(self, state: ReviewState, *, reason: str = "") -> Recommendation:
        return replace(self, state=state, suppression_reason=reason or self.suppression_reason)
