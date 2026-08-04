"""Population health: cohorts, prevalence, risk segmentation, and quality measures.

Analytics over a repository, always **within one organisation's boundary**. Crossing that
boundary needs an agreement covering population health or research as a purpose, and
de-identification on top rather than instead
(docs/design/adr-0030-cross-organisation-sharing.md).

Two things here are more careful than they look.

**A denominator is a decision, not a filter.** Quality measures have an initial population, a
denominator, exclusions, exceptions, and a numerator, and the difference between an *exclusion*
(the numerator event is not applicable — a bilateral mastectomy in a mammography measure) and an
*exception* (it was applicable and there was a documented reason not to do it) changes the
reported rate. Collapsing them, which is the common shortcut, misstates performance in the
direction that flatters.

**Small cells are suppressed.** A prevalence figure over three patients identifies them. The
threshold is a parameter with a stated default, and the suppressed cells are counted so a
consumer knows the total is not the sum of what they can see.
"""

from __future__ import annotations

import datetime as dt
import statistics
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from cip_core.logging import get_logger
from cip_interop.domain import InteropError
from cip_interop.fhir.repository import FhirRepository
from cip_interop.fhir.resources import Resource

__all__ = [
    "Cohort",
    "CohortDefinition",
    "MeasureResult",
    "PopulationError",
    "QualityMeasure",
    "RiskBand",
    "RiskSegmentation",
    "prevalence",
]

_log = get_logger(__name__)

#: Below this many members, a cell is suppressed rather than reported. Small-cell suppression is
#: the difference between a statistic and an identification.
DEFAULT_MIN_CELL_SIZE = 11


class PopulationError(InteropError):
    """A population query could not be answered."""


@dataclass(frozen=True, slots=True)
class CohortDefinition:
    """Who is in a cohort, expressed as predicates over resources.

    ``inclusion`` and ``exclusion`` are ordinary callables here because this is analyst-facing
    Python, not an operator-edited file — the distinction that governs the rules and mapping
    layers. A deployment exposing cohort building to non-developers would need the declarative
    treatment those layers get.
    """

    cohort_id: str
    name: str
    inclusion: Callable[[tuple[Resource, ...]], bool]
    exclusion: Callable[[tuple[Resource, ...]], bool] | None = None
    description: str = ""
    as_of: dt.date | None = None

    def includes(self, resources: tuple[Resource, ...]) -> bool:
        if not self.inclusion(resources):
            return False
        return not (self.exclusion and self.exclusion(resources))


@dataclass(frozen=True, slots=True)
class Cohort:
    """A resolved cohort."""

    definition: CohortDefinition
    patient_ids: tuple[str, ...]
    organization_id: str
    computed_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))
    considered: int = 0

    @property
    def size(self) -> int:
        return len(self.patient_ids)

    def reportable(self, min_cell_size: int = DEFAULT_MIN_CELL_SIZE) -> bool:
        return self.size >= min_cell_size

    def to_json(self, *, min_cell_size: int = DEFAULT_MIN_CELL_SIZE) -> dict[str, Any]:
        return {
            "cohort": self.definition.cohort_id,
            "name": self.definition.name,
            "organization": self.organization_id,
            "size": self.size if self.reportable(min_cell_size) else None,
            "suppressed": not self.reportable(min_cell_size),
            "considered": self.considered,
            "computed_at": self.computed_at.isoformat(),
        }


class RiskBand(StrEnum):
    """The population-management pyramid.

    ``RISING`` is the band that justifies the whole exercise: patients on their way to
    high-risk, where intervention is still cheap and effective. A segmentation that reports only
    high and low misses exactly the group worth acting on.
    """

    LOW = "low"
    RISING = "rising"
    HIGH = "high"
    COMPLEX = "complex"

    @property
    def is_actionable(self) -> bool:
        return self in (RiskBand.RISING, RiskBand.HIGH, RiskBand.COMPLEX)


@dataclass(frozen=True, slots=True)
class RiskSegmentation:
    """Patients bucketed into risk bands."""

    bands: dict[RiskBand, tuple[str, ...]]
    organization_id: str
    model_name: str = ""
    computed_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))

    @property
    def total(self) -> int:
        return sum(len(members) for members in self.bands.values())

    def share(self, band: RiskBand) -> float:
        return len(self.bands.get(band, ())) / self.total if self.total else 0.0

    def to_json(self, *, min_cell_size: int = DEFAULT_MIN_CELL_SIZE) -> dict[str, Any]:
        return {
            "model": self.model_name,
            "organization": self.organization_id,
            "total": self.total,
            "bands": {
                band.value: (len(members) if len(members) >= min_cell_size else None)
                for band, members in self.bands.items()
            },
            "suppressed_bands": [
                band.value
                for band, members in self.bands.items()
                if 0 < len(members) < min_cell_size
            ],
        }


def segment_by_condition_count(
    patients: dict[str, tuple[Resource, ...]],
    *,
    organization_id: str,
    rising_threshold: int = 2,
    high_threshold: int = 4,
    complex_threshold: int = 6,
) -> RiskSegmentation:
    """Band patients by active chronic condition count.

    A deliberately simple, transparent model. It is **not** a validated risk score, and it is
    named so in the output — an analytics layer that presents an unvalidated segmentation with
    the same weight as a validated one invites it to be used as one.
    """
    bands: dict[RiskBand, list[str]] = {band: [] for band in RiskBand}
    for patient_id, resources in patients.items():
        active = sum(
            1
            for r in resources
            if r.resource_type == "Condition"
            and not r.is_retracted
            and _clinical_status(r) in ("active", "recurrence", "relapse")
        )
        if active >= complex_threshold:
            bands[RiskBand.COMPLEX].append(patient_id)
        elif active >= high_threshold:
            bands[RiskBand.HIGH].append(patient_id)
        elif active >= rising_threshold:
            bands[RiskBand.RISING].append(patient_id)
        else:
            bands[RiskBand.LOW].append(patient_id)
    return RiskSegmentation(
        bands={band: tuple(sorted(members)) for band, members in bands.items()},
        organization_id=organization_id,
        model_name="active-chronic-condition-count (unvalidated, transparent)",
    )


def _clinical_status(resource: Resource) -> str:
    concept = resource.data.get("clinicalStatus") or {}
    if not isinstance(concept, dict):
        return ""
    for coding in concept.get("coding", []) or []:
        if isinstance(coding, dict) and isinstance(coding.get("code"), str):
            return coding["code"]
    return ""


def prevalence(
    patients: dict[str, tuple[Resource, ...]],
    condition_code: str,
    *,
    min_cell_size: int = DEFAULT_MIN_CELL_SIZE,
) -> dict[str, Any]:
    """Point prevalence of one condition.

    Returns the rate **and** the denominator. A rate without its denominator is unusable — 50%
    of four patients and 50% of four thousand are different facts — and reporting the rate alone
    is how a small-cohort artefact becomes a headline.
    """
    denominator = len(patients)
    if denominator == 0:
        return {"code": condition_code, "denominator": 0, "numerator": 0, "rate": None}
    numerator = sum(
        1
        for resources in patients.values()
        if any(
            r.resource_type == "Condition" and not r.is_retracted and _has_code(r, condition_code)
            for r in resources
        )
    )
    suppressed = 0 < numerator < min_cell_size
    return {
        "code": condition_code,
        "denominator": denominator,
        "numerator": None if suppressed else numerator,
        "rate": None if suppressed else round(numerator / denominator, 4),
        "suppressed": suppressed,
    }


def _has_code(resource: Resource, code: str) -> bool:
    concept = resource.data.get("code") or {}
    if not isinstance(concept, dict):
        return False
    return any(
        isinstance(c, dict) and c.get("code") == code for c in concept.get("coding", []) or []
    )


@dataclass(frozen=True, slots=True)
class QualityMeasure:
    """An electronic clinical quality measure.

    The five populations are separate because they mean different things and the arithmetic
    differs:

    ``rate = numerator / (denominator - exclusions - exceptions)``

    An **exclusion** removes a patient for whom the numerator event was never applicable. An
    **exception** removes one for whom it was applicable and there was a documented reason it
    did not happen. Collapsing them into one "excluded" bucket changes the reported rate and
    always in the flattering direction, because exceptions are the ones a provider documents.
    """

    measure_id: str
    title: str
    initial_population: Callable[[tuple[Resource, ...]], bool]
    denominator: Callable[[tuple[Resource, ...]], bool]
    numerator: Callable[[tuple[Resource, ...]], bool]
    denominator_exclusion: Callable[[tuple[Resource, ...]], bool] | None = None
    denominator_exception: Callable[[tuple[Resource, ...]], bool] | None = None
    version: str = "1.0.0"
    description: str = ""


@dataclass(frozen=True, slots=True)
class MeasureResult:
    """One measure computed over a population."""

    measure_id: str
    version: str
    initial_population: int
    denominator: int
    exclusions: int
    exceptions: int
    numerator: int
    organization_id: str = ""
    computed_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))

    @property
    def effective_denominator(self) -> int:
        return max(0, self.denominator - self.exclusions - self.exceptions)

    @property
    def rate(self) -> float | None:
        """``None`` when the effective denominator is zero.

        Not ``0.0``. A measure with nobody eligible has *no* performance rate, and reporting
        zero would put a provider at the bottom of a league table for having no eligible
        patients.
        """
        if self.effective_denominator == 0:
            return None
        return round(self.numerator / self.effective_denominator, 4)

    def reportable(self, min_cell_size: int = DEFAULT_MIN_CELL_SIZE) -> bool:
        return self.effective_denominator >= min_cell_size

    def to_json(self, *, min_cell_size: int = DEFAULT_MIN_CELL_SIZE) -> dict[str, Any]:
        reportable = self.reportable(min_cell_size)
        return {
            "measure": self.measure_id,
            "version": self.version,
            "organization": self.organization_id,
            "initial_population": self.initial_population,
            "denominator": self.denominator,
            "exclusions": self.exclusions,
            "exceptions": self.exceptions,
            "numerator": self.numerator if reportable else None,
            "effective_denominator": self.effective_denominator,
            "rate": self.rate if reportable else None,
            "suppressed": not reportable,
        }

    def render(self) -> str:
        rate = f"{self.rate:.1%}" if self.rate is not None else "not calculable"
        return (
            f"{self.measure_id}@{self.version}: {self.numerator}/"
            f"{self.effective_denominator} = {rate} "
            f"(IPP {self.initial_population}, excl {self.exclusions}, exc {self.exceptions})"
        )


class PopulationAnalytics:
    """Cohorts, prevalence, segmentation, and measures over one organisation."""

    def __init__(
        self, repository: FhirRepository, *, min_cell_size: int = DEFAULT_MIN_CELL_SIZE
    ) -> None:
        self._repository = repository
        self._min_cell_size = min_cell_size

    @property
    def organization_id(self) -> str:
        return self._repository.organization_id

    def patient_resources(self) -> dict[str, tuple[Resource, ...]]:
        """Every patient with their resources, within this organisation only."""
        patients = self._repository.search("Patient", count=100_000)
        return {p.id: self._repository.everything(p.id) for p in patients.resources}

    def resolve(self, definition: CohortDefinition) -> Cohort:
        population = self.patient_resources()
        members = tuple(
            sorted(pid for pid, resources in population.items() if definition.includes(resources))
        )
        _log.info(
            "population.cohort_resolved",
            cohort=definition.cohort_id,
            size=len(members),
            considered=len(population),
        )
        return Cohort(
            definition=definition,
            patient_ids=members,
            organization_id=self.organization_id,
            considered=len(population),
        )

    def compute(self, measure: QualityMeasure) -> MeasureResult:
        population = self.patient_resources()
        ipp = denominator = exclusions = exceptions = numerator = 0

        for resources in population.values():
            if not measure.initial_population(resources):
                continue
            ipp += 1
            if not measure.denominator(resources):
                continue
            denominator += 1
            if measure.denominator_exclusion and measure.denominator_exclusion(resources):
                exclusions += 1
                continue
            if measure.numerator(resources):
                numerator += 1
                continue
            # Exceptions are only counted for patients who did NOT meet the numerator. A
            # patient who received the care and also has a documented exception reason is a
            # numerator hit, not an exception — counting them as both inflates the rate.
            if measure.denominator_exception and measure.denominator_exception(resources):
                exceptions += 1

        return MeasureResult(
            measure_id=measure.measure_id,
            version=measure.version,
            initial_population=ipp,
            denominator=denominator,
            exclusions=exclusions,
            exceptions=exceptions,
            numerator=numerator,
            organization_id=self.organization_id,
        )

    def prevalence(self, condition_code: str) -> dict[str, Any]:
        return prevalence(
            self.patient_resources(), condition_code, min_cell_size=self._min_cell_size
        )

    def segment(self) -> RiskSegmentation:
        return segment_by_condition_count(
            self.patient_resources(), organization_id=self.organization_id
        )

    def longitudinal(self, patient_id: str, code: str) -> tuple[tuple[str, float], ...]:
        """One analyte over time for one patient, oldest first.

        Retracted observations are excluded. A withdrawn value left in a trend line changes the
        slope a clinician reads.
        """
        points: list[tuple[str, float]] = []
        for resource in self._repository.everything(patient_id):
            if resource.resource_type != "Observation" or resource.is_retracted:
                continue
            if not _has_code(resource, code):
                continue
            value = resource.get("valueQuantity.value")
            when = resource.get("effectiveDateTime", "")
            if isinstance(value, int | float) and isinstance(when, str) and when:
                points.append((when, float(value)))
        return tuple(sorted(points))

    def summary_statistics(self, patient_id: str, code: str) -> dict[str, Any]:
        series = self.longitudinal(patient_id, code)
        values = [v for _, v in series]
        if not values:
            return {"code": code, "count": 0}
        return {
            "code": code,
            "count": len(values),
            "first": values[0],
            "last": values[-1],
            "mean": round(statistics.fmean(values), 4),
            "min": min(values),
            "max": max(values),
        }
