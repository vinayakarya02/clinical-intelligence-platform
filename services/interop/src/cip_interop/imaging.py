"""Imaging: DICOM identity, PACS references, worklists, and the FHIR projection.

**No pixel data is read, decoded, stored, or transmitted here, and none should be.** This layer
models what is needed to *find* an image and to reason about the fact that it exists — which is
what clinical decision support and record assembly need — and leaves rendering, measurement, and
analysis to a PACS and a viewer that are validated for it.

The identity model is DICOM's, unchanged: study contains series contains instance, each with a
globally unique UID. Those UIDs are the join key between the imaging world and everything else,
so they are validated rather than trusted — a malformed UID is a reference that silently
resolves to nothing.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from cip_interop.domain import InteropError, ValidationError
from cip_interop.fhir.resources import Resource, fhir_id, make_reference

__all__ = [
    "DicomSeries",
    "DicomStudy",
    "ImagingError",
    "Modality",
    "PacsEndpoint",
    "WorklistItem",
    "to_imaging_study",
    "valid_uid",
]

#: A DICOM UID: dot-separated numeric components, at most 64 characters, no leading zeros in a
#: component (except the component "0" itself).
_UID = re.compile(r"^(0|[1-9]\d*)(\.(0|[1-9]\d*))*$")


class ImagingError(InteropError):
    """An imaging operation failed."""


def valid_uid(value: str) -> bool:
    """Whether a string is a well-formed DICOM UID.

    Checked rather than assumed. A UID with a trailing space — which real systems emit, because
    DICOM pads odd-length values — compares unequal to the same UID without one, so a study
    reference silently resolves to nothing and the images "disappear".
    """
    return bool(value) and len(value) <= 64 and bool(_UID.match(value))


class Modality(StrEnum):
    """DICOM modality codes.

    A closed set of the common ones. An unrecognised modality is retained as ``OT`` (other)
    with the original preserved, because refusing a study for an unusual modality would hide
    the existence of an image that was taken.
    """

    CT = "CT"
    MR = "MR"
    US = "US"
    CR = "CR"
    DX = "DX"
    XA = "XA"
    MG = "MG"
    NM = "NM"
    PT = "PT"
    RF = "RF"
    OT = "OT"

    @classmethod
    def parse(cls, code: str) -> Modality:
        try:
            return cls(code.strip().upper())
        except ValueError:
            return cls.OT

    @property
    def is_cross_sectional(self) -> bool:
        return self in (Modality.CT, Modality.MR, Modality.PT)


@dataclass(frozen=True, slots=True)
class PacsEndpoint:
    """Where images can be retrieved from.

    A DICOMweb base URL. Retrieval is the PACS's job; this records how to reach it so a
    reference is actionable rather than merely descriptive.
    """

    name: str
    wado_rs_base: str
    organization_id: str
    qido_rs_base: str = ""
    supports_stow: bool = False

    def __post_init__(self) -> None:
        if not self.wado_rs_base.startswith(("http://", "https://")):
            raise ImagingError(
                f"PACS endpoint {self.name!r} has a WADO-RS base that is not an absolute URL; "
                "a relative retrieval endpoint cannot be resolved by a viewer in another system"
            )

    def study_url(self, study_uid: str) -> str:
        if not valid_uid(study_uid):
            raise ImagingError(f"{study_uid!r} is not a valid DICOM UID")
        return f"{self.wado_rs_base.rstrip('/')}/studies/{study_uid}"

    def series_url(self, study_uid: str, series_uid: str) -> str:
        return f"{self.study_url(study_uid)}/series/{series_uid}"


@dataclass(frozen=True, slots=True)
class DicomInstance:
    """One image or object. Identity only — the pixels stay in the PACS."""

    sop_instance_uid: str
    sop_class_uid: str = ""
    instance_number: int | None = None

    def __post_init__(self) -> None:
        if not valid_uid(self.sop_instance_uid):
            raise ValidationError(f"SOP Instance UID {self.sop_instance_uid!r} is malformed")


@dataclass(frozen=True, slots=True)
class DicomSeries:
    """One acquisition within a study."""

    series_instance_uid: str
    modality: Modality
    series_number: int | None = None
    description: str = ""
    body_site: str = ""
    laterality: str = ""
    instances: tuple[DicomInstance, ...] = ()
    started: dt.datetime | None = None

    def __post_init__(self) -> None:
        if not valid_uid(self.series_instance_uid):
            raise ValidationError(f"Series Instance UID {self.series_instance_uid!r} is malformed")

    @property
    def instance_count(self) -> int:
        return len(self.instances)


@dataclass(frozen=True, slots=True)
class DicomStudy:
    """One imaging study.

    ``patient_reference`` is required. An orphan study — one whose patient is unknown — is a
    real and dangerous state in imaging (it happens when a modality is used without a worklist
    entry), and it must be represented as a *failure to attribute* rather than as a study
    quietly attached to whoever was matched last.
    """

    study_instance_uid: str
    patient_reference: str
    organization_id: str
    accession_number: str = ""
    started: dt.datetime | None = None
    description: str = ""
    referring_physician: str = ""
    series: tuple[DicomSeries, ...] = ()
    endpoint: PacsEndpoint | None = None
    based_on_order: str = ""
    status: str = "available"

    def __post_init__(self) -> None:
        if not valid_uid(self.study_instance_uid):
            raise ValidationError(f"Study Instance UID {self.study_instance_uid!r} is malformed")
        if not self.patient_reference.strip():
            raise ValidationError(
                f"study {self.study_instance_uid} has no patient reference. An unattributed "
                "study must be held for reconciliation, not attached to a guess."
            )

    @property
    def modalities(self) -> tuple[Modality, ...]:
        seen: list[Modality] = []
        for series in self.series:
            if series.modality not in seen:
                seen.append(series.modality)
        return tuple(seen)

    @property
    def instance_count(self) -> int:
        return sum(s.instance_count for s in self.series)

    def retrieve_url(self) -> str:
        if self.endpoint is None:
            return ""
        return self.endpoint.study_url(self.study_instance_uid)


def to_imaging_study(study: DicomStudy, *, resource_id: str = "") -> Resource:
    """Project a DICOM study into a FHIR ``ImagingStudy``.

    The identifier is the study UID in its ``urn:oid:`` form, which is how FHIR carries a DICOM
    UID and what makes the resource joinable back to the PACS.
    """
    data: dict[str, Any] = {
        "resourceType": "ImagingStudy",
        "id": fhir_id(resource_id or f"is-{study.study_instance_uid}"),
        "status": study.status,
        "identifier": [{"system": "urn:dicom:uid", "value": f"urn:oid:{study.study_instance_uid}"}],
        "subject": make_reference(*study.patient_reference.split("/", 1)),
        "numberOfSeries": len(study.series),
        "numberOfInstances": study.instance_count,
    }
    if study.accession_number:
        data["identifier"].append(
            {
                "type": {"coding": [{"code": "ACSN"}]},
                "system": "urn:accession",
                "value": study.accession_number,
            }
        )
    if study.started:
        data["started"] = study.started.isoformat()
    if study.modalities:
        data["modality"] = [
            {"system": "http://dicom.nema.org/resources/ontology/DCM", "code": m.value}
            for m in study.modalities
        ]
    if study.based_on_order:
        data["basedOn"] = [make_reference("ServiceRequest", study.based_on_order)]
    if study.series:
        data["series"] = [
            {
                "uid": s.series_instance_uid,
                "number": s.series_number,
                "modality": {"code": s.modality.value},
                "description": s.description,
                "numberOfInstances": s.instance_count,
                "bodySite": {"text": s.body_site} if s.body_site else None,
            }
            for s in study.series
        ]
        for entry in data["series"]:
            for key in [k for k, v in entry.items() if v is None]:
                del entry[key]

    return Resource(resource_type="ImagingStudy", data=data, organization_id=study.organization_id)


class WorklistStatus(StrEnum):
    """Where a worklist item has got to.

    Mirrors DICOM UPS: scheduled, in progress, completed, cancelled.
    """

    SCHEDULED = "SCHEDULED"
    IN_PROGRESS = "IN PROGRESS"
    COMPLETED = "COMPLETED"
    CANCELED = "CANCELED"

    @property
    def is_terminal(self) -> bool:
        return self in (WorklistStatus.COMPLETED, WorklistStatus.CANCELED)


@dataclass(frozen=True, slots=True)
class WorklistItem:
    """One scheduled procedure step.

    The modality worklist is what prevents orphan studies: a technologist selecting the patient
    from a worklist gets the identifiers right, and one typing them at the console does not.
    Modelling it is therefore an identity control, not a convenience.
    """

    accession_number: str
    patient_reference: str
    modality: Modality
    scheduled_at: dt.datetime
    organization_id: str
    procedure_description: str = ""
    service_request_id: str = ""
    status: WorklistStatus = WorklistStatus.SCHEDULED
    station_ae_title: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "accession": self.accession_number,
            "patient": self.patient_reference,
            "modality": self.modality.value,
            "scheduled": self.scheduled_at.isoformat(),
            "status": self.status.value,
            "procedure": self.procedure_description,
            "order": self.service_request_id,
        }


@dataclass(slots=True)
class ImagingRegistry:
    """Studies, worklists, and the reconciliation queue between them."""

    studies: dict[str, DicomStudy] = field(default_factory=dict)
    worklist: dict[str, WorklistItem] = field(default_factory=dict)
    #: Studies whose accession number matches no worklist item. Held rather than attached — an
    #: unreconciled study belongs to a queue a human works, not to a patient a matcher guessed.
    unreconciled: list[tuple[str, str]] = field(default_factory=list)

    def schedule(self, item: WorklistItem) -> None:
        self.worklist[item.accession_number] = item

    def register_study(self, study: DicomStudy) -> bool:
        """Record a study, reconciling it against the worklist.

        Returns ``True`` when the study reconciled. An unreconciled study is still recorded —
        the images exist and hiding that helps nobody — but it is flagged, because the patient
        attribution on it came from the modality console rather than from the order.
        """
        self.studies[study.study_instance_uid] = study
        if not study.accession_number:
            self.unreconciled.append((study.study_instance_uid, "no accession number"))
            return False
        item = self.worklist.get(study.accession_number)
        if item is None:
            self.unreconciled.append(
                (study.study_instance_uid, f"accession {study.accession_number} is not scheduled")
            )
            return False
        if item.patient_reference != study.patient_reference:
            self.unreconciled.append(
                (
                    study.study_instance_uid,
                    f"patient mismatch: worklist says {item.patient_reference}, study says "
                    f"{study.patient_reference}",
                )
            )
            return False
        return True

    def studies_for(self, patient_reference: str) -> tuple[DicomStudy, ...]:
        return tuple(s for s in self.studies.values() if s.patient_reference == patient_reference)

    def statistics(self) -> dict[str, Any]:
        return {
            "studies": len(self.studies),
            "series": sum(len(s.series) for s in self.studies.values()),
            "instances": sum(s.instance_count for s in self.studies.values()),
            "worklist_items": len(self.worklist),
            "unreconciled": len(self.unreconciled),
        }
