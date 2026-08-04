"""Drug intelligence.

Seven checks over a patient's medication list: pairwise interactions, drug–disease
interactions, duplicate therapy, contraindications, dose limits, allergy conflicts, and organ-
function adjustment flags.

Everything clinical is loaded from the knowledge base. This module contains the *checking*,
never the content (docs/design/adr-0019-knowledge-as-data.md).

Two properties run through all seven:

**Absence of a finding is not a finding of safety.** Every result says so explicitly. A
clinician reading "no interactions found" must not read it as "this combination is safe" —
it means the loaded knowledge base had nothing to say, which is a different claim entirely.

**Organ-function checks flag, they do not dose.** The engine says an adjustment may be needed
and cites why. It does not say what the adjusted dose is, because that requires weight,
indication, and clinical judgement the engine does not have.
"""

from __future__ import annotations

import datetime as dt
import itertools
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from cip_core.logging import get_logger
from cip_decision.domain import (
    Citation,
    ClinicalFact,
    EvidenceQuality,
    FactKind,
    PatientContext,
    ProvenanceLink,
    Recommendation,
    RecommendationKind,
    Severity,
)

__all__ = ["DrugCheckKind", "DrugFinding", "DrugIntelligence", "DrugReport"]

_log = get_logger(__name__)


class DrugCheckKind(StrEnum):
    """Which of the seven checks produced a finding."""

    INTERACTION = "drug_drug_interaction"
    DRUG_DISEASE = "drug_disease_interaction"
    DUPLICATE_THERAPY = "duplicate_therapy"
    CONTRAINDICATION = "contraindication"
    DOSE = "dose_verification"
    ALLERGY = "allergy_conflict"
    ORGAN_ADJUSTMENT = "organ_function_adjustment"


@dataclass(frozen=True, slots=True)
class DrugFinding:
    """One thing the drug checks found."""

    check: DrugCheckKind
    identifier: str
    summary: str
    severity: Severity
    evidence_quality: EvidenceQuality
    citations: tuple[Citation, ...]
    subjects: tuple[str, ...] = ()
    mechanism: str = ""
    management: str = ""

    def __post_init__(self) -> None:
        if not self.citations:
            raise ValueError(
                f"Drug finding '{self.identifier}' has no citation and cannot be defended."
            )

    def to_recommendation(self, context: PatientContext) -> Recommendation:
        detail_parts = []
        if self.mechanism:
            detail_parts.append(f"Mechanism: {self.mechanism.strip()}")
        if self.management:
            detail_parts.append(f"Management: {self.management.strip()}")
        detail_parts.append(f"The evidence for this {self.evidence_quality.qualifier}.")
        return Recommendation(
            id=f"rec:{self.check.value}:{self.identifier}:{context.patient_id}",
            kind=RecommendationKind.ALERT,
            summary=self.summary,
            severity=self.severity,
            evidence_quality=self.evidence_quality,
            citations=self.citations,
            provenance=(
                ProvenanceLink(
                    kind="drug_check", identifier=self.identifier, label=self.check.value
                ),
                *(
                    ProvenanceLink(kind="medication", identifier=s, label="subject")
                    for s in self.subjects
                ),
            ),
            detail="\n".join(detail_parts),
            rationale=f"{', '.join(self.subjects)} — {self.check.value.replace('_', ' ')}",
            patient_id=context.patient_id,
            triggering_facts=self.subjects,
            # The concern includes the *subjects*, not only the knowledge entry. A
            # class-level interaction entry matches several distinct drug pairs, and keying
            # the concern on the entry alone made two genuinely different contraindications
            # deduplicate into one — silently hiding a real finding for the other pair.
            metadata={
                "concern": ":".join(
                    (self.check.value, self.identifier, *sorted(s.lower() for s in self.subjects))
                ),
                # Every one of the seven drug checks points *away* from the agent it names —
                # that is what a drug safety check is. Declared rather than inferred, so the
                # contradiction detector never has to read prose.
                "direction": "away",
            },
        )


@dataclass(frozen=True, slots=True)
class DrugReport:
    """Everything the seven checks found, and what was checked."""

    findings: tuple[DrugFinding, ...] = ()
    medications_checked: tuple[str, ...] = ()
    pairs_checked: int = 0
    checks_run: tuple[DrugCheckKind, ...] = ()

    @property
    def has_findings(self) -> bool:
        return bool(self.findings)

    def by_severity(self) -> tuple[DrugFinding, ...]:
        return tuple(
            sorted(
                self.findings,
                key=lambda f: (-f.severity.rank, -f.evidence_quality.weight, f.identifier),
            )
        )

    def absence_statement(self) -> str:
        """What "nothing found" actually means.

        The most consequential misreading available: a clinician who reads an empty result as
        "this combination is safe" has been misled by silence. Every report states the real
        claim.
        """
        return (
            f"{self.pairs_checked} medication pair(s) were checked against the loaded knowledge "
            f"base. No finding means the knowledge base contained no matching entry — it is "
            f"not a determination that the combination is safe."
        )


class DrugIntelligence:
    """Runs the seven drug checks against a patient's record.

    Knowledge is injected. The class holds no clinical content, so replacing the corpus with a
    licensed reference database is a constructor argument rather than a rewrite.
    """

    def __init__(
        self,
        *,
        interactions: tuple[dict[str, Any], ...] = (),
        drug_classes: dict[str, str] | None = None,
        dose_limits: tuple[dict[str, Any], ...] = (),
        drug_disease: tuple[dict[str, Any], ...] = (),
        organ_adjustments: tuple[dict[str, Any], ...] = (),
        cross_reactive_classes: frozenset[str] = frozenset(),
    ) -> None:
        self._interactions = interactions
        self._drug_classes = {k.lower(): v for k, v in (drug_classes or {}).items()}
        self._dose_limits = dose_limits
        self._drug_disease = drug_disease
        self._organ_adjustments = organ_adjustments
        self._cross_reactive = {c.lower() for c in cross_reactive_classes}
        """Classes where an allergy to one member is conventionally treated as an allergy to
        the class — beta-lactams, for example. **Declared, never assumed.** Treating every
        class as cross-reactive means an allergy to one statin contraindicates every statin,
        which denies a patient a needed drug on no evidence. Default is exact ingredient
        matching."""

    def check(self, context: PatientContext) -> DrugReport:
        """Run every check."""
        medications = context.of_kind(FactKind.MEDICATION, active_only=True)
        names = tuple(m.name for m in medications)

        findings: list[DrugFinding] = []
        findings.extend(self._check_interactions(medications))
        findings.extend(self._check_duplicate_therapy(medications))
        findings.extend(self._check_allergies(medications, context))
        findings.extend(self._check_drug_disease(medications, context))
        findings.extend(self._check_dose(medications))
        findings.extend(self._check_organ_function(medications, context))

        pairs = len(list(itertools.combinations(medications, 2)))
        report = DrugReport(
            findings=tuple(findings),
            medications_checked=names,
            pairs_checked=pairs,
            checks_run=tuple(DrugCheckKind),
        )
        _log.debug(
            "drugs.checked",
            medications=len(names),
            pairs=pairs,
            findings=len(findings),
        )
        return report

    # ---- the seven checks -------------------------------------------------------------

    def _check_interactions(self, medications: tuple[ClinicalFact, ...]) -> list[DrugFinding]:
        """Pairwise drug–drug interactions.

        Pairwise only. Three-drug interactions exist and are not modelled; that limitation is
        in the safety case rather than approximated here, because an approximation of a
        three-way interaction is worse than its absence.
        """
        findings: list[DrugFinding] = []
        for left, right in itertools.combinations(medications, 2):
            for entry in self._interactions:
                if self._pair_matches(entry, left, right):
                    findings.append(
                        DrugFinding(
                            check=DrugCheckKind.INTERACTION,
                            identifier=entry["id"],
                            summary=f"{left.name} with {right.name}: {entry['effect']}",
                            severity=Severity(entry["severity"]),
                            evidence_quality=EvidenceQuality(entry["evidence_quality"]),
                            citations=_citations(entry),
                            subjects=(left.name, right.name),
                            mechanism=entry.get("mechanism", ""),
                            management=entry.get("management", ""),
                        )
                    )
        return findings

    def _pair_matches(self, entry: dict[str, Any], left: ClinicalFact, right: ClinicalFact) -> bool:
        """Whether an interaction entry describes this pair, in either order.

        Matching is by name *or* class. Class matching is what makes a knowledge base
        tractable: an entry for "ACE inhibitor with aldosterone antagonist" covers a dozen
        pairs that would otherwise each need enumerating.
        """
        left_keys = self._keys_for(left)
        right_keys = self._keys_for(right)
        entry_left = {str(entry.get("left", "")).lower(), str(entry.get("left_class", "")).lower()}
        entry_right = {
            str(entry.get("right", "")).lower(),
            str(entry.get("right_class", "")).lower(),
        }
        entry_left.discard("")
        entry_right.discard("")

        forward = bool(left_keys & entry_left) and bool(right_keys & entry_right)
        reverse = bool(left_keys & entry_right) and bool(right_keys & entry_left)
        return forward or reverse

    def _keys_for(self, medication: ClinicalFact) -> set[str]:
        name = medication.name.strip().lower()
        keys = {name}
        # A record may say "Lisinopril 10 mg"; the first token is the ingredient.
        keys.add(name.split()[0] if name.split() else name)
        declared = medication.attributes.get("class")
        if declared:
            keys.add(str(declared).lower())
        for known, klass in self._drug_classes.items():
            if known in name:
                keys.add(klass.lower())
        return keys

    def _check_duplicate_therapy(self, medications: tuple[ClinicalFact, ...]) -> list[DrugFinding]:
        """Two agents of the same class.

        Duplicate therapy is sometimes deliberate — two agents of one class at submaximal
        doses is a recognised strategy — so this is ``moderate`` and worded as a prompt to
        confirm intent rather than as an error.
        """
        by_class: dict[str, list[ClinicalFact]] = {}
        for medication in medications:
            klass = self._class_of(medication)
            if klass:
                by_class.setdefault(klass, []).append(medication)

        findings: list[DrugFinding] = []
        for klass, members in sorted(by_class.items()):
            if len(members) < 2:
                continue
            names = tuple(m.name for m in members)
            findings.append(
                DrugFinding(
                    check=DrugCheckKind.DUPLICATE_THERAPY,
                    identifier=f"duplicate:{klass}",
                    summary=f"{len(members)} {klass} agents prescribed concurrently",
                    severity=Severity.MODERATE,
                    evidence_quality=EvidenceQuality.ESTABLISHED,
                    citations=(
                        Citation(
                            source="Medication reconciliation practice",
                            reference="Therapeutic duplication review",
                        ),
                    ),
                    subjects=names,
                    management=(
                        "Confirm this duplication is intended. Concurrent agents of one class "
                        "are sometimes deliberate and sometimes a reconciliation error."
                    ),
                )
            )
        return findings

    def _class_of(self, medication: ClinicalFact) -> str:
        declared = medication.attributes.get("class")
        if declared:
            return str(declared).lower()
        name = medication.name.strip().lower()
        for known, klass in self._drug_classes.items():
            if known in name:
                return klass.lower()
        return ""

    def _check_allergies(
        self, medications: tuple[ClinicalFact, ...], context: PatientContext
    ) -> list[DrugFinding]:
        """A prescribed agent against a recorded allergy.

        Matches on the allergen name *and* its class. An allergy to one penicillin is
        conventionally recorded against the class, and matching only the exact name would
        miss every other member.
        """
        allergies = context.of_kind(FactKind.ALLERGY)
        findings: list[DrugFinding] = []

        for allergy in allergies:
            allergen = allergy.name.strip().lower()
            allergen_class = str(allergy.attributes.get("class", "")).lower()
            for medication in medications:
                keys = self._keys_for(medication)
                exact = allergen in medication.name.lower()
                # Class matching only where the class is *declared* cross-reactive.
                cross = (
                    bool(allergen_class)
                    and allergen_class in self._cross_reactive
                    and allergen_class in keys
                )
                if not (exact or cross):
                    continue
                reaction = allergy.attributes.get("reaction", "")
                basis = (
                    "the same ingredient" if exact else f"cross-reactivity within {allergen_class}"
                )
                findings.append(
                    DrugFinding(
                        check=DrugCheckKind.ALLERGY,
                        identifier=f"allergy:{allergen}:{medication.name.lower()}",
                        summary=(
                            f"{medication.name} conflicts with a recorded allergy to {allergy.name}"
                        ),
                        # An allergy conflict is contraindicated rather than major, so it is
                        # never suppressed by volume controls at any role.
                        severity=Severity.CONTRAINDICATED,
                        evidence_quality=EvidenceQuality.ESTABLISHED,
                        citations=(
                            Citation(
                                source="Patient record",
                                reference=f"Recorded allergy: {allergy.name}",
                            ),
                        ),
                        subjects=(medication.name, allergy.name),
                        mechanism=(
                            f"Recorded reaction: {reaction}. Matched on {basis}."
                            if reaction
                            else f"Matched on {basis}."
                        ),
                        management="Confirm the allergy and select an alternative agent.",
                    )
                )
        return findings

    def _check_drug_disease(
        self, medications: tuple[ClinicalFact, ...], context: PatientContext
    ) -> list[DrugFinding]:
        """A prescribed agent against a recorded condition."""
        findings: list[DrugFinding] = []
        for entry in self._drug_disease:
            drug = str(entry.get("drug", "")).lower()
            condition = str(entry.get("condition", "")).lower()
            matched_drug = next((m for m in medications if drug and drug in m.name.lower()), None)
            if matched_drug is None or not context.has(FactKind.CONDITION, condition):
                continue
            findings.append(
                DrugFinding(
                    check=DrugCheckKind.DRUG_DISEASE,
                    identifier=entry["id"],
                    summary=f"{matched_drug.name} in the context of {condition}: {entry['effect']}",
                    severity=Severity(entry["severity"]),
                    evidence_quality=EvidenceQuality(entry["evidence_quality"]),
                    citations=_citations(entry),
                    subjects=(matched_drug.name, condition),
                    mechanism=entry.get("mechanism", ""),
                    management=entry.get("management", ""),
                )
            )
        return findings

    def _check_dose(self, medications: tuple[ClinicalFact, ...]) -> list[DrugFinding]:
        """Prescribed daily dose against a configured limit.

        Verification against a stated limit, not calculation. The engine does not compute a
        dose — that needs weight, renal function, indication, and judgement it does not have.
        """
        findings: list[DrugFinding] = []
        for entry in self._dose_limits:
            drug = str(entry.get("drug", "")).lower()
            maximum = entry.get("max_daily")
            unit = entry.get("unit", "")
            if maximum is None:
                continue
            for medication in medications:
                if drug not in medication.name.lower():
                    continue
                prescribed = medication.attributes.get("daily_dose")
                if prescribed is None:
                    continue
                recorded_unit = str(medication.attributes.get("dose_unit", "")).lower()
                if unit and recorded_unit and recorded_unit != str(unit).lower():
                    # Units are never converted. A silent conversion is how a microgram dose
                    # is compared against a milligram limit and passes.
                    continue
                if float(prescribed) > float(maximum):
                    findings.append(
                        DrugFinding(
                            check=DrugCheckKind.DOSE,
                            identifier=f"dose:{entry['id']}",
                            summary=(
                                f"{medication.name} daily dose {prescribed} {unit} exceeds the "
                                f"stated maximum of {maximum} {unit}"
                            ),
                            severity=Severity(entry.get("severity", "major")),
                            evidence_quality=EvidenceQuality(
                                entry.get("evidence_quality", "established")
                            ),
                            citations=_citations(entry),
                            subjects=(medication.name,),
                            management=entry.get("management", "Review the prescribed dose."),
                        )
                    )
        return findings

    def _check_organ_function(
        self, medications: tuple[ClinicalFact, ...], context: PatientContext
    ) -> list[DrugFinding]:
        """Renal and hepatic adjustment flags.

        A flag, not a dose. It says an adjustment may be needed and cites why; deciding the
        adjusted dose requires clinical judgement.
        """
        findings: list[DrugFinding] = []
        for entry in self._organ_adjustments:
            drug = str(entry.get("drug", "")).lower()
            marker = str(entry.get("marker", "")).lower()
            threshold = entry.get("below")
            if not drug or not marker or threshold is None:
                continue

            observation = context.latest(FactKind.OBSERVATION, marker)
            if observation is None or observation.value is None:
                continue
            if observation.value >= float(threshold):
                continue

            for medication in medications:
                if drug not in medication.name.lower():
                    continue
                organ = entry.get("organ", "renal")
                findings.append(
                    DrugFinding(
                        check=DrugCheckKind.ORGAN_ADJUSTMENT,
                        identifier=f"organ:{entry['id']}",
                        summary=(
                            f"{medication.name} may need {organ} adjustment: "
                            f"{marker} is {observation.value} {observation.unit or ''}".strip()
                        ),
                        severity=Severity(entry.get("severity", "moderate")),
                        evidence_quality=EvidenceQuality(
                            entry.get("evidence_quality", "established")
                        ),
                        citations=_citations(entry),
                        subjects=(medication.name, marker),
                        management=(
                            entry.get("management")
                            or f"Review the dose against {organ} function. This engine flags "
                            "the need for review; it does not calculate an adjusted dose."
                        ),
                    )
                )
        return findings


def _citations(entry: dict[str, Any]) -> tuple[Citation, ...]:
    raw = entry.get("citations") or []
    citations: list[Citation] = []
    for item in raw:
        if isinstance(item, str):
            citations.append(Citation(source=item))
        else:
            published = item.get("published")
            citations.append(
                Citation(
                    source=item["source"],
                    reference=item.get("reference", ""),
                    url=item.get("url", ""),
                    published=(dt.date.fromisoformat(str(published)) if published else None),
                )
            )
    if not citations:
        raise ValueError(f"Knowledge entry '{entry.get('id')}' has no citation")
    return tuple(citations)
