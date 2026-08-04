"""Structured clinical data behind a protocol.

The tools need a patient's conditions, medications, observations, and encounters. Where those
live is a deployment question — PostgreSQL in this platform, an EHR's FHIR API in a hospital
integration, a de-identified warehouse for research — so the tools depend on this protocol
and never on a store.

The records are FHIR-shaped without being FHIR objects. Field names follow the resources they
correspond to (``Condition.onset``, ``MedicationStatement.effective``, ``Observation.value``),
so the FHIR renderer is a projection rather than a translation, but nothing here requires a
FHIR library or accepts a FHIR document's full complexity. The subset is what clinical
reasoning in this phase actually consumes.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

__all__ = [
    "ClinicalDataSource",
    "ConditionRecord",
    "EncounterRecord",
    "InMemoryClinicalData",
    "MedicationRecord",
    "ObservationRecord",
    "PatientRecord",
]


@dataclass(frozen=True, slots=True)
class PatientRecord:
    """Demographics only. Never carries clinical content.

    Separated so a tool that needs an age for a risk score does not incidentally read a
    problem list, and so the audit log can distinguish the two accesses.
    """

    patient_id: uuid.UUID
    tenant_id: uuid.UUID
    display_name: str
    birth_date: dt.date | None = None
    sex: str | None = None
    mrn: str | None = None

    def age_on(self, when: dt.date) -> int | None:
        """Age in whole years, or ``None`` if the birth date is unknown."""
        if self.birth_date is None:
            return None
        years = when.year - self.birth_date.year
        if (when.month, when.day) < (self.birth_date.month, self.birth_date.day):
            years -= 1
        return max(0, years)


@dataclass(frozen=True, slots=True)
class ConditionRecord:
    """A diagnosis. Mirrors FHIR ``Condition``."""

    condition_id: str
    patient_id: uuid.UUID
    display: str
    code: str | None = None
    code_system: str | None = None
    onset: dt.date | None = None
    abatement: dt.date | None = None
    clinical_status: str = "active"
    source_document_id: uuid.UUID | None = None

    @property
    def is_active(self) -> bool:
        return self.clinical_status == "active" and self.abatement is None


@dataclass(frozen=True, slots=True)
class MedicationRecord:
    """A medication. Mirrors FHIR ``MedicationStatement``."""

    medication_id: str
    patient_id: uuid.UUID
    display: str
    rxnorm_code: str | None = None
    dose: str | None = None
    frequency: str | None = None
    start_date: dt.date | None = None
    end_date: dt.date | None = None
    status: str = "active"
    source_document_id: uuid.UUID | None = None

    @property
    def is_active(self) -> bool:
        return self.status == "active" and self.end_date is None


@dataclass(frozen=True, slots=True)
class ObservationRecord:
    """A lab or vital. Mirrors FHIR ``Observation``."""

    observation_id: str
    patient_id: uuid.UUID
    display: str
    value: float
    unit: str
    effective: dt.date
    loinc_code: str | None = None
    reference_low: float | None = None
    reference_high: float | None = None
    source_document_id: uuid.UUID | None = None

    @property
    def flag(self) -> str:
        """``low`` / ``high`` / ``normal`` / ``unknown`` against the reference range.

        ``unknown`` when no range is recorded, rather than defaulting to normal: an
        unflagged abnormal value reads as reassurance the data does not support.
        """
        if self.reference_low is None and self.reference_high is None:
            return "unknown"
        if self.reference_low is not None and self.value < self.reference_low:
            return "low"
        if self.reference_high is not None and self.value > self.reference_high:
            return "high"
        return "normal"

    def describe(self) -> str:
        text = f"{self.display} {self.value} {self.unit} on {self.effective.isoformat()}"
        flag = self.flag
        return f"{text} ({flag})" if flag not in ("normal", "unknown") else text


@dataclass(frozen=True, slots=True)
class EncounterRecord:
    """A visit or admission. Mirrors FHIR ``Encounter``."""

    encounter_id: str
    patient_id: uuid.UUID
    kind: str
    start: dt.date
    end: dt.date | None = None
    reason: str | None = None
    source_document_id: uuid.UUID | None = None


@runtime_checkable
class ClinicalDataSource(Protocol):
    """Reads structured clinical facts for one tenant."""

    async def get_patient(
        self, patient_id: uuid.UUID, *, tenant_id: uuid.UUID
    ) -> PatientRecord | None: ...

    async def get_conditions(
        self, patient_id: uuid.UUID, *, tenant_id: uuid.UUID
    ) -> list[ConditionRecord]: ...

    async def get_medications(
        self, patient_id: uuid.UUID, *, tenant_id: uuid.UUID
    ) -> list[MedicationRecord]: ...

    async def get_observations(
        self, patient_id: uuid.UUID, *, tenant_id: uuid.UUID, display: str | None = None
    ) -> list[ObservationRecord]: ...

    async def get_encounters(
        self, patient_id: uuid.UUID, *, tenant_id: uuid.UUID
    ) -> list[EncounterRecord]: ...


@dataclass
class InMemoryClinicalData:
    """In-process clinical store for development, tests, and the demo.

    Enforces tenant scoping on every read rather than trusting callers, so an isolation bug
    fails here instead of only against a real database — the same reasoning as the in-memory
    vector and graph stores in Phase 2.
    """

    patients: dict[uuid.UUID, PatientRecord] = field(default_factory=dict)
    conditions: list[ConditionRecord] = field(default_factory=list)
    medications: list[MedicationRecord] = field(default_factory=list)
    observations: list[ObservationRecord] = field(default_factory=list)
    encounters: list[EncounterRecord] = field(default_factory=list)

    def add_patient(self, patient: PatientRecord) -> None:
        self.patients[patient.patient_id] = patient

    def _visible(self, patient_id: uuid.UUID, tenant_id: uuid.UUID) -> bool:
        patient = self.patients.get(patient_id)
        return patient is not None and patient.tenant_id == tenant_id

    async def get_patient(
        self, patient_id: uuid.UUID, *, tenant_id: uuid.UUID
    ) -> PatientRecord | None:
        patient = self.patients.get(patient_id)
        if patient is None or patient.tenant_id != tenant_id:
            return None
        return patient

    async def get_conditions(
        self, patient_id: uuid.UUID, *, tenant_id: uuid.UUID
    ) -> list[ConditionRecord]:
        if not self._visible(patient_id, tenant_id):
            return []
        return [c for c in self.conditions if c.patient_id == patient_id]

    async def get_medications(
        self, patient_id: uuid.UUID, *, tenant_id: uuid.UUID
    ) -> list[MedicationRecord]:
        if not self._visible(patient_id, tenant_id):
            return []
        return [m for m in self.medications if m.patient_id == patient_id]

    async def get_observations(
        self, patient_id: uuid.UUID, *, tenant_id: uuid.UUID, display: str | None = None
    ) -> list[ObservationRecord]:
        if not self._visible(patient_id, tenant_id):
            return []
        found = [o for o in self.observations if o.patient_id == patient_id]
        if display:
            needle = display.strip().lower()
            found = [o for o in found if needle in o.display.lower()]
        return sorted(found, key=lambda o: o.effective)

    async def get_encounters(
        self, patient_id: uuid.UUID, *, tenant_id: uuid.UUID
    ) -> list[EncounterRecord]:
        if not self._visible(patient_id, tenant_id):
            return []
        return sorted(
            (e for e in self.encounters if e.patient_id == patient_id), key=lambda e: e.start
        )
