"""Alert suppression.

The safety feature this phase most depends on. Published CDS override rates run 49–96%, and
roughly 300 reminders are needed to prevent one adverse drug event — so a system that fires on
everything is *worse than no system*, because it trains clinicians to dismiss the alert that
mattered (docs/design/adr-0021-alert-fatigue.md).

Four mechanisms, in the order they apply:

1. **Deduplication** by clinical concern — two rules reaching the same conclusion are one
   alert with two supports.
2. **Override memory** — a recommendation this clinician rejected for this patient does not
   return unchanged.
3. **Severity floor per role** — a prescriber and a pharmacist see different things, which the
   systematic reviews identify as the single most effective intervention.
4. **Volume ceiling** — above it, the lowest-severity items fold into one summary.

``CONTRAINDICATED`` is exempt from every one of them.

Every suppression is recorded with its reason. Suppression that cannot be audited is
indistinguishable from a bug.
"""

from __future__ import annotations

import datetime as dt
from collections import OrderedDict
from dataclasses import dataclass, replace
from enum import StrEnum

from cip_core.logging import get_logger
from cip_decision.domain import Recommendation, ReviewState, Severity

__all__ = ["ClinicalRole", "SuppressionPolicy", "SuppressionResult", "Suppressor"]

_log = get_logger(__name__)


class ClinicalRole(StrEnum):
    """Who is receiving the alert.

    Role tailoring is the intervention the systematic reviews found most effective: a
    pharmacist reviewing a medication list wants moderate interactions; a prescriber mid-order
    does not, and showing them trains the reflex that loses the major one.
    """

    PRESCRIBER = "prescriber"
    PHARMACIST = "pharmacist"
    NURSE = "nurse"
    REVIEWER = "reviewer"
    UNKNOWN = "unknown"

    @property
    def default_floor(self) -> Severity:
        """The lowest severity this role sees by default.

        ``UNKNOWN`` sees everything. Guessing a role wrongly in the *restrictive* direction
        hides a major interaction from someone who needed it, which is the worse error.
        """
        return {
            "prescriber": Severity.MAJOR,
            "pharmacist": Severity.MODERATE,
            "nurse": Severity.MAJOR,
            "reviewer": Severity.MINOR,
            "unknown": Severity.INFORMATIONAL,
        }[self.value]


@dataclass(frozen=True, slots=True)
class SuppressionPolicy:
    """Tunable suppression behaviour.

    Defaults come from the alert-fatigue literature, not from any institution's risk appetite.
    An operator is expected to set these deliberately.
    """

    role: ClinicalRole = ClinicalRole.UNKNOWN
    severity_floor: Severity | None = None
    max_alerts: int = 8
    override_memory_days: int = 30
    deduplicate: bool = True

    def floor(self) -> Severity:
        return self.severity_floor or self.role.default_floor


@dataclass(frozen=True, slots=True)
class SuppressionResult:
    """What survived, what did not, and why."""

    shown: tuple[Recommendation, ...] = ()
    suppressed: tuple[Recommendation, ...] = ()
    summary_line: str = ""

    @property
    def suppression_rate(self) -> float:
        total = len(self.shown) + len(self.suppressed)
        return round(len(self.suppressed) / total, 4) if total else 0.0

    def reasons(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self.suppressed:
            counts[item.suppression_reason] = counts.get(item.suppression_reason, 0) + 1
        return counts


@dataclass(frozen=True, slots=True)
class OverrideRecord:
    """A clinician's rejection, remembered."""

    recommendation_id: str
    patient_id: str
    reason: str
    at: dt.datetime


class Suppressor:
    """Applies the suppression policy to a recommendation set."""

    def __init__(
        self, *, policy: SuppressionPolicy | None = None, max_overrides: int = 20_000
    ) -> None:
        self._policy = policy or SuppressionPolicy()
        # Bounded: one entry per rejected recommendation per patient, which in a busy service
        # grows steadily. Entries older than the memory window are useless anyway, so LRU
        # eviction loses nothing that was still being consulted.
        self._overrides: OrderedDict[tuple[str, str], OverrideRecord] = OrderedDict()
        self._max_overrides = max_overrides

    def remember_override(
        self, recommendation: Recommendation, *, reason: str, at: dt.datetime | None = None
    ) -> None:
        """Record that a clinician rejected this recommendation for this patient.

        The most valuable datum the system collects: the only direct signal that a rule is
        wrong for this patient, and the input to mechanism 2.
        """
        key = (recommendation.id, str(recommendation.patient_id))
        self._overrides[key] = OverrideRecord(
            recommendation_id=recommendation.id,
            patient_id=str(recommendation.patient_id),
            reason=reason,
            at=at or dt.datetime.now(dt.UTC),
        )
        self._overrides.move_to_end(key)
        while len(self._overrides) > self._max_overrides:
            self._overrides.popitem(last=False)

    def apply(
        self, recommendations: tuple[Recommendation, ...], *, now: dt.datetime | None = None
    ) -> SuppressionResult:
        """Decide what a clinician actually sees."""
        moment = now or dt.datetime.now(dt.UTC)
        policy = self._policy

        working = list(recommendations)
        suppressed: list[Recommendation] = []

        if policy.deduplicate:
            working, folded = self._deduplicate(working)
            suppressed.extend(folded)

        working, overridden = self._drop_overridden(working, moment)
        suppressed.extend(overridden)

        working, below_floor = self._apply_floor(working, policy.floor())
        suppressed.extend(below_floor)

        working.sort(key=lambda r: (-r.severity.rank, -r.evidence_quality.weight, r.id))

        summary_line = ""
        if len(working) > policy.max_alerts:
            keep, fold = self._apply_ceiling(working, policy.max_alerts)
            working = keep
            suppressed.extend(fold)
            if fold:
                # Folded, not dropped: a suppressed alert must remain discoverable, so the
                # count and the highest folded severity are surfaced.
                highest = max(f.severity for f in fold)
                summary_line = (
                    f"{len(fold)} further finding(s) were folded into this summary; the "
                    f"highest was {highest.value}."
                )

        result = SuppressionResult(
            shown=tuple(working), suppressed=tuple(suppressed), summary_line=summary_line
        )
        if suppressed:
            _log.info(
                "suppression.applied",
                role=str(policy.role),
                shown=len(result.shown),
                suppressed=len(result.suppressed),
                reasons=result.reasons(),
            )
        return result

    # ---- mechanisms ------------------------------------------------------------------

    @staticmethod
    def _deduplicate(
        recommendations: list[Recommendation],
    ) -> tuple[list[Recommendation], list[Recommendation]]:
        """Fold recommendations sharing a clinical concern into one.

        Only recommendations that *declare* a concern deduplicate. Two without one are never
        merged — inferring that two alerts mean the same thing from their text would
        occasionally merge two genuinely different concerns, which is a safety regression.
        """
        by_concern: dict[str, list[Recommendation]] = {}
        standalone: list[Recommendation] = []

        for item in recommendations:
            concern = str(item.metadata.get("concern", ""))
            if concern:
                by_concern.setdefault(concern, []).append(item)
            else:
                standalone.append(item)

        kept: list[Recommendation] = list(standalone)
        folded: list[Recommendation] = []

        for concern, group in sorted(by_concern.items()):
            if len(group) == 1:
                kept.append(group[0])
                continue
            # Keep the most severe, and attach every supporting rule to it so the merge does
            # not lose evidence.
            group.sort(key=lambda r: (-r.severity.rank, -r.evidence_quality.weight, r.id))
            primary, rest = group[0], group[1:]
            supports = tuple(
                dict.fromkeys(
                    primary.source_rule_ids
                    + tuple(rid for other in rest for rid in other.source_rule_ids)
                )
            )
            kept.append(
                replace(
                    primary,
                    source_rule_ids=supports,
                    detail=(
                        primary.detail
                        + f"\n\nSupported by {len(supports)} rule(s) reaching the same "
                        f"conclusion ({concern})."
                    ).strip(),
                )
            )
            folded.extend(
                r.with_state(ReviewState.SUPPRESSED, reason=f"deduplicated into {primary.id}")
                for r in rest
            )
        return kept, folded

    def _drop_overridden(
        self, recommendations: list[Recommendation], now: dt.datetime
    ) -> tuple[list[Recommendation], list[Recommendation]]:
        """Remove what this clinician already rejected for this patient."""
        kept: list[Recommendation] = []
        dropped: list[Recommendation] = []
        window = dt.timedelta(days=self._policy.override_memory_days)

        for item in recommendations:
            if not item.severity.is_suppressible:
                kept.append(item)
                continue
            record = self._overrides.get((item.id, str(item.patient_id)))
            if record is not None and (now - record.at) <= window:
                dropped.append(
                    item.with_state(
                        ReviewState.SUPPRESSED,
                        reason=f"previously rejected: {record.reason}",
                    )
                )
            else:
                kept.append(item)
        return kept, dropped

    @staticmethod
    def _apply_floor(
        recommendations: list[Recommendation], floor: Severity
    ) -> tuple[list[Recommendation], list[Recommendation]]:
        """Hide what is below this role's severity floor."""
        kept: list[Recommendation] = []
        dropped: list[Recommendation] = []
        for item in recommendations:
            if not item.severity.is_suppressible or item.severity.rank >= floor.rank:
                kept.append(item)
            else:
                dropped.append(
                    item.with_state(
                        ReviewState.SUPPRESSED,
                        reason=f"below the {floor.value} floor for this role",
                    )
                )
        return kept, dropped

    @staticmethod
    def _apply_ceiling(
        recommendations: list[Recommendation], ceiling: int
    ) -> tuple[list[Recommendation], list[Recommendation]]:
        """Fold the lowest-severity overflow into a summary.

        Contraindications are never folded, even past the ceiling — so a patient with nine
        contraindications sees nine, which is the correct outcome however unusual.
        """
        protected = [r for r in recommendations if not r.severity.is_suppressible]
        rest = [r for r in recommendations if r.severity.is_suppressible]

        room = max(0, ceiling - len(protected))
        keep = protected + rest[:room]
        fold = [
            r.with_state(ReviewState.SUPPRESSED, reason="folded into the volume summary")
            for r in rest[room:]
        ]
        keep.sort(key=lambda r: (-r.severity.rank, -r.evidence_quality.weight, r.id))
        return keep, fold
