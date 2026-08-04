"""FHIR resource definitions.

FHIR is defined by ``StructureDefinition`` resources — element paths, cardinalities, types, and
value-set bindings — not by a class per resource. This models it the same way, for one
practical reason: a hand-written class per resource has its cardinality rules in code that
nobody diffs against the specification, and the first person to add an optional field will
quietly make a required one optional.

Element definitions are data, so "what does this platform require on an Observation" is a
question with a printable answer.

Both R4 and R5 are supported from one definition set. Elements that exist in only one version
declare it, because the R4→R5 differences on these resources are real and silently accepting an
R4 element on an R5 resource means writing a field no R5 client will read.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

__all__ = [
    "ElementDefinition",
    "ElementType",
    "FhirVersion",
    "ResourceDefinition",
    "definition_for",
    "supported_resource_types",
]


class FhirVersion(StrEnum):
    """The FHIR releases this gateway serves.

    Negotiated by content type rather than by URL path, because the FHIR version and this
    platform's own API version change on different schedules
    (docs/design/adr-0032-api-surface-and-versioning.md).
    """

    R4 = "4.0.1"
    R5 = "5.0.0"

    @property
    def mime_parameter(self) -> str:
        return "4.0" if self is FhirVersion.R4 else "5.0"


class ElementType(StrEnum):
    """The subset of FHIR datatypes these resources use."""

    STRING = "string"
    CODE = "code"
    URI = "uri"
    ID = "id"
    BOOLEAN = "boolean"
    INTEGER = "integer"
    DECIMAL = "decimal"
    DATE = "date"
    DATETIME = "dateTime"
    INSTANT = "instant"
    IDENTIFIER = "Identifier"
    HUMAN_NAME = "HumanName"
    ADDRESS = "Address"
    CONTACT_POINT = "ContactPoint"
    CODEABLE_CONCEPT = "CodeableConcept"
    CODING = "Coding"
    QUANTITY = "Quantity"
    PERIOD = "Period"
    REFERENCE = "Reference"
    CODEABLE_REFERENCE = "CodeableReference"
    BACKBONE = "BackboneElement"
    ATTACHMENT = "Attachment"

    @property
    def is_primitive(self) -> bool:
        return self in {
            ElementType.STRING,
            ElementType.CODE,
            ElementType.URI,
            ElementType.ID,
            ElementType.BOOLEAN,
            ElementType.INTEGER,
            ElementType.DECIMAL,
            ElementType.DATE,
            ElementType.DATETIME,
            ElementType.INSTANT,
        }


@dataclass(frozen=True, slots=True)
class ElementDefinition:
    """One element of a resource.

    ``max_cardinality`` is a string because FHIR writes it that way — ``"1"`` or ``"*"`` — and
    translating it to an integer sentinel loses the distinction between "exactly one" and
    "unbounded" at exactly the place a validator needs it.
    """

    path: str
    type: ElementType
    min_cardinality: int = 0
    max_cardinality: str = "1"
    binding: frozenset[str] = frozenset()
    """Required binding. A non-empty set means the value **must** be one of these codes; an
    unbound coded element accepts anything, which is how a status of ``activ`` gets stored."""
    reference_targets: frozenset[str] = frozenset()
    """Resource types a Reference may point at. Enforced, because a Reference to the wrong
    resource type is a dangling pointer that no consumer discovers until it dereferences it."""
    versions: frozenset[FhirVersion] = frozenset({FhirVersion.R4, FhirVersion.R5})
    choice_of: str = ""
    """For ``value[x]``-style choice elements: the shared prefix. Exactly one member of a
    choice group may be present, and the validator enforces that rather than trusting it."""
    summary: bool = False
    """Whether this element appears in a summary (``_summary=true``) representation."""

    @property
    def name(self) -> str:
        return self.path.split(".")[-1]

    @property
    def is_required(self) -> bool:
        return self.min_cardinality > 0

    @property
    def is_repeating(self) -> bool:
        return self.max_cardinality != "1"

    def applies_to(self, version: FhirVersion) -> bool:
        return version in self.versions

    def render(self) -> str:
        return f"{self.path} {self.min_cardinality}..{self.max_cardinality} {self.type.value}"


@dataclass(frozen=True, slots=True)
class ResourceDefinition:
    """What a resource type looks like."""

    resource_type: str
    elements: tuple[ElementDefinition, ...]
    description: str = ""
    #: Search parameters this gateway actually implements. The CapabilityStatement is generated
    #: from this, so it can only understate — a client cannot be told about a search that does
    #: not work (docs/design/adr-0032-api-surface-and-versioning.md).
    search_parameters: tuple[str, ...] = ()
    #: The element holding the patient reference, for consent scoping and stream partitioning.
    #: Empty for resources that are not patient-specific.
    patient_element: str = ""

    def for_version(self, version: FhirVersion) -> tuple[ElementDefinition, ...]:
        return tuple(e for e in self.elements if e.applies_to(version))

    def element(self, name: str, version: FhirVersion) -> ElementDefinition | None:
        for definition in self.for_version(version):
            if definition.name == name:
                return definition
        return None

    def choice_groups(self, version: FhirVersion) -> dict[str, tuple[ElementDefinition, ...]]:
        groups: dict[str, list[ElementDefinition]] = {}
        for definition in self.for_version(version):
            if definition.choice_of:
                groups.setdefault(definition.choice_of, []).append(definition)
        return {k: tuple(v) for k, v in groups.items()}

    def to_json(self) -> dict[str, Any]:
        return {
            "resourceType": self.resource_type,
            "elements": [e.render() for e in self.elements],
            "searchParameters": list(self.search_parameters),
        }


def _e(
    path: str,
    type_: ElementType,
    minimum: int = 0,
    maximum: str = "1",
    **kwargs: Any,
) -> ElementDefinition:
    return ElementDefinition(
        path=path, type=type_, min_cardinality=minimum, max_cardinality=maximum, **kwargs
    )


T = ElementType
R4_ONLY = frozenset({FhirVersion.R4})
R5_ONLY = frozenset({FhirVersion.R5})

#: Statuses that mark a resource as no longer asserting what it says. Used by the repository to
#: decide what a search returns by default, because returning entered-in-error records as
#: current is how a retracted result gets acted on.
RETRACTED_STATUSES = frozenset({"entered-in-error", "cancelled", "revoked", "nullified"})

_DEFINITIONS: dict[str, ResourceDefinition] = {
    "Patient": ResourceDefinition(
        resource_type="Patient",
        description="A person receiving care",
        search_parameters=("identifier", "family", "given", "birthdate", "gender", "_id"),
        elements=(
            _e("Patient.id", T.ID, summary=True),
            _e("Patient.identifier", T.IDENTIFIER, 0, "*", summary=True),
            _e("Patient.active", T.BOOLEAN, summary=True),
            _e("Patient.name", T.HUMAN_NAME, 0, "*", summary=True),
            _e("Patient.telecom", T.CONTACT_POINT, 0, "*"),
            _e(
                "Patient.gender",
                T.CODE,
                binding=frozenset({"male", "female", "other", "unknown"}),
                summary=True,
            ),
            _e("Patient.birthDate", T.DATE, summary=True),
            _e("Patient.deceasedBoolean", T.BOOLEAN, choice_of="deceased"),
            _e("Patient.deceasedDateTime", T.DATETIME, choice_of="deceased"),
            _e("Patient.address", T.ADDRESS, 0, "*"),
            _e(
                "Patient.managingOrganization",
                T.REFERENCE,
                reference_targets=frozenset({"Organization"}),
            ),
        ),
    ),
    "Organization": ResourceDefinition(
        resource_type="Organization",
        description="A hospital, lab, imaging centre, or pharmacy",
        search_parameters=("identifier", "name", "type", "_id"),
        elements=(
            _e("Organization.id", T.ID, summary=True),
            _e("Organization.identifier", T.IDENTIFIER, 0, "*", summary=True),
            _e("Organization.active", T.BOOLEAN, summary=True),
            _e("Organization.type", T.CODEABLE_CONCEPT, 0, "*"),
            _e("Organization.name", T.STRING, 1, summary=True),
            _e(
                "Organization.partOf",
                T.REFERENCE,
                reference_targets=frozenset({"Organization"}),
                summary=True,
            ),
        ),
    ),
    "Practitioner": ResourceDefinition(
        resource_type="Practitioner",
        description="A clinician",
        search_parameters=("identifier", "family", "given", "_id"),
        elements=(
            _e("Practitioner.id", T.ID, summary=True),
            _e("Practitioner.identifier", T.IDENTIFIER, 0, "*", summary=True),
            _e("Practitioner.active", T.BOOLEAN, summary=True),
            _e("Practitioner.name", T.HUMAN_NAME, 0, "*", summary=True),
            _e("Practitioner.telecom", T.CONTACT_POINT, 0, "*"),
        ),
    ),
    "Encounter": ResourceDefinition(
        resource_type="Encounter",
        description="An interaction between a patient and a provider",
        search_parameters=("patient", "status", "date", "identifier", "_id"),
        patient_element="subject",
        elements=(
            _e("Encounter.id", T.ID, summary=True),
            _e("Encounter.identifier", T.IDENTIFIER, 0, "*", summary=True),
            _e(
                "Encounter.status",
                T.CODE,
                1,
                binding=frozenset(
                    {
                        "planned",
                        "arrived",
                        "triaged",
                        "in-progress",
                        "onleave",
                        "finished",
                        "cancelled",
                        "entered-in-error",
                        "unknown",
                    }
                ),
                summary=True,
            ),
            # R4 has a single Coding; R5 widened it to a repeating CodeableConcept. Modelled as
            # two elements rather than one lenient one, so an R4 payload cannot smuggle an R5
            # shape past validation.
            _e("Encounter.class", T.CODING, 1, versions=R4_ONLY, summary=True),
            _e("Encounter.class", T.CODEABLE_CONCEPT, 0, "*", versions=R5_ONLY, summary=True),
            _e(
                "Encounter.subject",
                T.REFERENCE,
                1,
                reference_targets=frozenset({"Patient"}),
                summary=True,
            ),
            _e("Encounter.period", T.PERIOD, versions=R4_ONLY, summary=True),
            _e("Encounter.actualPeriod", T.PERIOD, versions=R5_ONLY, summary=True),
            _e("Encounter.reasonCode", T.CODEABLE_CONCEPT, 0, "*", versions=R4_ONLY),
            _e(
                "Encounter.serviceProvider",
                T.REFERENCE,
                reference_targets=frozenset({"Organization"}),
            ),
        ),
    ),
    "Condition": ResourceDefinition(
        resource_type="Condition",
        description="A clinical problem or diagnosis",
        search_parameters=("patient", "code", "clinical-status", "onset-date", "_id"),
        patient_element="subject",
        elements=(
            _e("Condition.id", T.ID, summary=True),
            _e("Condition.identifier", T.IDENTIFIER, 0, "*"),
            _e(
                "Condition.clinicalStatus",
                T.CODEABLE_CONCEPT,
                binding=frozenset(
                    {"active", "recurrence", "relapse", "inactive", "remission", "resolved"}
                ),
                summary=True,
            ),
            _e(
                "Condition.verificationStatus",
                T.CODEABLE_CONCEPT,
                binding=frozenset(
                    {
                        "unconfirmed",
                        "provisional",
                        "differential",
                        "confirmed",
                        "refuted",
                        "entered-in-error",
                    }
                ),
                summary=True,
            ),
            _e("Condition.category", T.CODEABLE_CONCEPT, 0, "*"),
            _e("Condition.code", T.CODEABLE_CONCEPT, 1, summary=True),
            _e(
                "Condition.subject",
                T.REFERENCE,
                1,
                reference_targets=frozenset({"Patient"}),
                summary=True,
            ),
            _e(
                "Condition.encounter",
                T.REFERENCE,
                reference_targets=frozenset({"Encounter"}),
            ),
            _e("Condition.onsetDateTime", T.DATETIME, choice_of="onset", summary=True),
            _e("Condition.onsetPeriod", T.PERIOD, choice_of="onset", summary=True),
            _e("Condition.recordedDate", T.DATETIME),
        ),
    ),
    "Observation": ResourceDefinition(
        resource_type="Observation",
        description="A measurement or assertion",
        search_parameters=("patient", "code", "category", "date", "status", "_id"),
        patient_element="subject",
        elements=(
            _e("Observation.id", T.ID, summary=True),
            _e("Observation.identifier", T.IDENTIFIER, 0, "*"),
            _e(
                "Observation.status",
                T.CODE,
                1,
                binding=frozenset(
                    {
                        "registered",
                        "preliminary",
                        "final",
                        "amended",
                        "corrected",
                        "cancelled",
                        "entered-in-error",
                        "unknown",
                    }
                ),
                summary=True,
            ),
            _e("Observation.category", T.CODEABLE_CONCEPT, 0, "*"),
            _e("Observation.code", T.CODEABLE_CONCEPT, 1, summary=True),
            _e(
                "Observation.subject",
                T.REFERENCE,
                1,
                reference_targets=frozenset({"Patient"}),
                summary=True,
            ),
            _e(
                "Observation.encounter",
                T.REFERENCE,
                reference_targets=frozenset({"Encounter"}),
            ),
            _e("Observation.effectiveDateTime", T.DATETIME, choice_of="effective", summary=True),
            _e("Observation.effectivePeriod", T.PERIOD, choice_of="effective", summary=True),
            _e("Observation.issued", T.INSTANT),
            _e("Observation.valueQuantity", T.QUANTITY, choice_of="value", summary=True),
            _e(
                "Observation.valueCodeableConcept",
                T.CODEABLE_CONCEPT,
                choice_of="value",
                summary=True,
            ),
            _e("Observation.valueString", T.STRING, choice_of="value", summary=True),
            _e("Observation.valueBoolean", T.BOOLEAN, choice_of="value", summary=True),
            _e("Observation.dataAbsentReason", T.CODEABLE_CONCEPT),
            _e("Observation.interpretation", T.CODEABLE_CONCEPT, 0, "*"),
            _e("Observation.referenceRange", T.BACKBONE, 0, "*"),
        ),
    ),
    "Medication": ResourceDefinition(
        resource_type="Medication",
        description="A drug product",
        search_parameters=("code", "identifier", "_id"),
        elements=(
            _e("Medication.id", T.ID, summary=True),
            _e("Medication.identifier", T.IDENTIFIER, 0, "*"),
            _e("Medication.code", T.CODEABLE_CONCEPT, 0, "1", summary=True),
            _e(
                "Medication.status",
                T.CODE,
                binding=frozenset({"active", "inactive", "entered-in-error"}),
            ),
            _e("Medication.form", T.CODEABLE_CONCEPT),
        ),
    ),
    "MedicationRequest": ResourceDefinition(
        resource_type="MedicationRequest",
        description="An order or prescription for a medication",
        search_parameters=("patient", "status", "intent", "authoredon", "_id"),
        patient_element="subject",
        elements=(
            _e("MedicationRequest.id", T.ID, summary=True),
            _e("MedicationRequest.identifier", T.IDENTIFIER, 0, "*"),
            _e(
                "MedicationRequest.status",
                T.CODE,
                1,
                binding=frozenset(
                    {
                        "active",
                        "on-hold",
                        "cancelled",
                        "completed",
                        "entered-in-error",
                        "stopped",
                        "draft",
                        "unknown",
                    }
                ),
                summary=True,
            ),
            _e(
                "MedicationRequest.intent",
                T.CODE,
                1,
                binding=frozenset(
                    {
                        "proposal",
                        "plan",
                        "order",
                        "original-order",
                        "reflex-order",
                        "filler-order",
                        "instance-order",
                        "option",
                    }
                ),
                summary=True,
            ),
            # The most consequential R4/R5 difference in this set: R4 has a medication[x]
            # choice, R5 replaced it with a single CodeableReference. A gateway that accepted
            # either on either version would produce resources the counterparty cannot read.
            _e(
                "MedicationRequest.medicationCodeableConcept",
                T.CODEABLE_CONCEPT,
                choice_of="medication",
                versions=R4_ONLY,
                summary=True,
            ),
            _e(
                "MedicationRequest.medicationReference",
                T.REFERENCE,
                choice_of="medication",
                reference_targets=frozenset({"Medication"}),
                versions=R4_ONLY,
                summary=True,
            ),
            _e(
                "MedicationRequest.medication",
                T.CODEABLE_REFERENCE,
                1,
                versions=R5_ONLY,
                reference_targets=frozenset({"Medication"}),
                summary=True,
            ),
            _e(
                "MedicationRequest.subject",
                T.REFERENCE,
                1,
                reference_targets=frozenset({"Patient"}),
                summary=True,
            ),
            _e(
                "MedicationRequest.encounter",
                T.REFERENCE,
                reference_targets=frozenset({"Encounter"}),
            ),
            _e("MedicationRequest.authoredOn", T.DATETIME, summary=True),
            _e(
                "MedicationRequest.requester",
                T.REFERENCE,
                reference_targets=frozenset({"Practitioner", "Organization"}),
                summary=True,
            ),
            _e("MedicationRequest.dosageInstruction", T.BACKBONE, 0, "*"),
        ),
    ),
    "AllergyIntolerance": ResourceDefinition(
        resource_type="AllergyIntolerance",
        description="A propensity for an adverse reaction",
        search_parameters=("patient", "code", "clinical-status", "_id"),
        patient_element="patient",
        elements=(
            _e("AllergyIntolerance.id", T.ID, summary=True),
            _e("AllergyIntolerance.identifier", T.IDENTIFIER, 0, "*"),
            _e(
                "AllergyIntolerance.clinicalStatus",
                T.CODEABLE_CONCEPT,
                binding=frozenset({"active", "inactive", "resolved"}),
                summary=True,
            ),
            _e(
                "AllergyIntolerance.verificationStatus",
                T.CODEABLE_CONCEPT,
                binding=frozenset(
                    {"unconfirmed", "presumed", "confirmed", "refuted", "entered-in-error"}
                ),
                summary=True,
            ),
            _e(
                "AllergyIntolerance.type",
                T.CODE,
                binding=frozenset({"allergy", "intolerance"}),
                versions=R4_ONLY,
            ),
            _e(
                "AllergyIntolerance.criticality",
                T.CODE,
                binding=frozenset({"low", "high", "unable-to-assess"}),
                summary=True,
            ),
            _e("AllergyIntolerance.code", T.CODEABLE_CONCEPT, 1, summary=True),
            _e(
                "AllergyIntolerance.patient",
                T.REFERENCE,
                1,
                reference_targets=frozenset({"Patient"}),
                summary=True,
            ),
            _e("AllergyIntolerance.reaction", T.BACKBONE, 0, "*"),
        ),
    ),
    "Procedure": ResourceDefinition(
        resource_type="Procedure",
        description="An action performed on a patient",
        search_parameters=("patient", "code", "date", "status", "_id"),
        patient_element="subject",
        elements=(
            _e("Procedure.id", T.ID, summary=True),
            _e("Procedure.identifier", T.IDENTIFIER, 0, "*"),
            _e(
                "Procedure.status",
                T.CODE,
                1,
                binding=frozenset(
                    {
                        "preparation",
                        "in-progress",
                        "not-done",
                        "on-hold",
                        "stopped",
                        "completed",
                        "entered-in-error",
                        "unknown",
                    }
                ),
                summary=True,
            ),
            _e("Procedure.code", T.CODEABLE_CONCEPT, 1, summary=True),
            _e(
                "Procedure.subject",
                T.REFERENCE,
                1,
                reference_targets=frozenset({"Patient"}),
                summary=True,
            ),
            _e("Procedure.performedDateTime", T.DATETIME, choice_of="performed", summary=True),
            _e("Procedure.performedPeriod", T.PERIOD, choice_of="performed", summary=True),
            _e(
                "Procedure.encounter",
                T.REFERENCE,
                reference_targets=frozenset({"Encounter"}),
            ),
        ),
    ),
    "DiagnosticReport": ResourceDefinition(
        resource_type="DiagnosticReport",
        description="Findings from a diagnostic investigation",
        search_parameters=("patient", "code", "category", "date", "status", "_id"),
        patient_element="subject",
        elements=(
            _e("DiagnosticReport.id", T.ID, summary=True),
            _e("DiagnosticReport.identifier", T.IDENTIFIER, 0, "*", summary=True),
            _e(
                "DiagnosticReport.status",
                T.CODE,
                1,
                binding=frozenset(
                    {
                        "registered",
                        "partial",
                        "preliminary",
                        "final",
                        "amended",
                        "corrected",
                        "appended",
                        "cancelled",
                        "entered-in-error",
                        "unknown",
                    }
                ),
                summary=True,
            ),
            _e("DiagnosticReport.category", T.CODEABLE_CONCEPT, 0, "*"),
            _e("DiagnosticReport.code", T.CODEABLE_CONCEPT, 1, summary=True),
            _e(
                "DiagnosticReport.subject",
                T.REFERENCE,
                1,
                reference_targets=frozenset({"Patient"}),
                summary=True,
            ),
            _e("DiagnosticReport.effectiveDateTime", T.DATETIME, choice_of="effective"),
            _e("DiagnosticReport.issued", T.INSTANT, summary=True),
            _e(
                "DiagnosticReport.result",
                T.REFERENCE,
                0,
                "*",
                reference_targets=frozenset({"Observation"}),
            ),
            _e(
                "DiagnosticReport.imagingStudy",
                T.REFERENCE,
                0,
                "*",
                reference_targets=frozenset({"ImagingStudy"}),
                versions=R4_ONLY,
            ),
            _e("DiagnosticReport.conclusion", T.STRING),
        ),
    ),
    "ServiceRequest": ResourceDefinition(
        resource_type="ServiceRequest",
        description="An order for a service — lab, imaging, referral",
        search_parameters=("patient", "code", "status", "intent", "authored", "_id"),
        patient_element="subject",
        elements=(
            _e("ServiceRequest.id", T.ID, summary=True),
            _e("ServiceRequest.identifier", T.IDENTIFIER, 0, "*", summary=True),
            _e(
                "ServiceRequest.status",
                T.CODE,
                1,
                binding=frozenset(
                    {
                        "draft",
                        "active",
                        "on-hold",
                        "revoked",
                        "completed",
                        "entered-in-error",
                        "unknown",
                    }
                ),
                summary=True,
            ),
            _e(
                "ServiceRequest.intent",
                T.CODE,
                1,
                binding=frozenset(
                    {
                        "proposal",
                        "plan",
                        "directive",
                        "order",
                        "original-order",
                        "reflex-order",
                        "filler-order",
                        "instance-order",
                        "option",
                    }
                ),
                summary=True,
            ),
            _e("ServiceRequest.category", T.CODEABLE_CONCEPT, 0, "*"),
            _e(
                "ServiceRequest.priority",
                T.CODE,
                binding=frozenset({"routine", "urgent", "asap", "stat"}),
                summary=True,
            ),
            _e("ServiceRequest.code", T.CODEABLE_CONCEPT, 0, "1", summary=True),
            _e(
                "ServiceRequest.subject",
                T.REFERENCE,
                1,
                reference_targets=frozenset({"Patient"}),
                summary=True,
            ),
            _e(
                "ServiceRequest.encounter",
                T.REFERENCE,
                reference_targets=frozenset({"Encounter"}),
            ),
            _e("ServiceRequest.authoredOn", T.DATETIME, summary=True),
            _e(
                "ServiceRequest.requester",
                T.REFERENCE,
                reference_targets=frozenset({"Practitioner", "Organization"}),
                summary=True,
            ),
            _e(
                "ServiceRequest.performer",
                T.REFERENCE,
                0,
                "*",
                reference_targets=frozenset({"Practitioner", "Organization"}),
                summary=True,
            ),
        ),
    ),
    "CarePlan": ResourceDefinition(
        resource_type="CarePlan",
        description="A plan for a patient's care",
        search_parameters=("patient", "status", "category", "_id"),
        patient_element="subject",
        elements=(
            _e("CarePlan.id", T.ID, summary=True),
            _e("CarePlan.identifier", T.IDENTIFIER, 0, "*", summary=True),
            _e(
                "CarePlan.status",
                T.CODE,
                1,
                binding=frozenset(
                    {
                        "draft",
                        "active",
                        "on-hold",
                        "revoked",
                        "completed",
                        "entered-in-error",
                        "unknown",
                    }
                ),
                summary=True,
            ),
            _e(
                "CarePlan.intent",
                T.CODE,
                1,
                binding=frozenset({"proposal", "plan", "order", "option", "directive"}),
                summary=True,
            ),
            _e("CarePlan.category", T.CODEABLE_CONCEPT, 0, "*"),
            _e("CarePlan.title", T.STRING, summary=True),
            _e(
                "CarePlan.subject",
                T.REFERENCE,
                1,
                reference_targets=frozenset({"Patient"}),
                summary=True,
            ),
            _e("CarePlan.period", T.PERIOD, summary=True),
            _e("CarePlan.activity", T.BACKBONE, 0, "*"),
        ),
    ),
    "DocumentReference": ResourceDefinition(
        resource_type="DocumentReference",
        description="Metadata about a clinical document; the XDS DocumentEntry equivalent",
        search_parameters=("patient", "type", "category", "date", "status", "_id"),
        patient_element="subject",
        elements=(
            _e("DocumentReference.id", T.ID, summary=True),
            _e("DocumentReference.masterIdentifier", T.IDENTIFIER, versions=R4_ONLY),
            _e("DocumentReference.identifier", T.IDENTIFIER, 0, "*", summary=True),
            _e(
                "DocumentReference.status",
                T.CODE,
                1,
                binding=frozenset({"current", "superseded", "entered-in-error"}),
                summary=True,
            ),
            _e("DocumentReference.type", T.CODEABLE_CONCEPT, summary=True),
            _e("DocumentReference.category", T.CODEABLE_CONCEPT, 0, "*"),
            _e(
                "DocumentReference.subject",
                T.REFERENCE,
                reference_targets=frozenset({"Patient"}),
                summary=True,
            ),
            _e("DocumentReference.date", T.INSTANT, summary=True),
            _e(
                "DocumentReference.author",
                T.REFERENCE,
                0,
                "*",
                reference_targets=frozenset({"Practitioner", "Organization"}),
                summary=True,
            ),
            _e("DocumentReference.content", T.BACKBONE, 1, "*", summary=True),
            # R4 nests encounter and period inside a context BackboneElement; R5 flattens
            # context to a list of references and moves the period out.
            _e("DocumentReference.context", T.BACKBONE, versions=R4_ONLY),
            _e(
                "DocumentReference.context",
                T.REFERENCE,
                0,
                "*",
                reference_targets=frozenset({"Encounter"}),
                versions=R5_ONLY,
            ),
            _e("DocumentReference.period", T.PERIOD, versions=R5_ONLY),
        ),
    ),
    "ImagingStudy": ResourceDefinition(
        resource_type="ImagingStudy",
        description="A DICOM study; identity and retrieval, never pixels",
        search_parameters=("patient", "status", "modality", "started", "identifier", "_id"),
        patient_element="subject",
        elements=(
            _e("ImagingStudy.id", T.ID, summary=True),
            _e("ImagingStudy.identifier", T.IDENTIFIER, 1, "*", summary=True),
            _e(
                "ImagingStudy.status",
                T.CODE,
                1,
                binding=frozenset(
                    {"registered", "available", "cancelled", "entered-in-error", "unknown"}
                ),
                summary=True,
            ),
            _e("ImagingStudy.modality", T.CODING, 0, "*", versions=R4_ONLY, summary=True),
            _e(
                "ImagingStudy.modality",
                T.CODEABLE_CONCEPT,
                0,
                "*",
                versions=R5_ONLY,
                summary=True,
            ),
            _e(
                "ImagingStudy.subject",
                T.REFERENCE,
                1,
                reference_targets=frozenset({"Patient"}),
                summary=True,
            ),
            _e("ImagingStudy.started", T.DATETIME, summary=True),
            _e(
                "ImagingStudy.basedOn",
                T.REFERENCE,
                0,
                "*",
                reference_targets=frozenset({"ServiceRequest"}),
            ),
            _e("ImagingStudy.numberOfSeries", T.INTEGER, summary=True),
            _e("ImagingStudy.numberOfInstances", T.INTEGER, summary=True),
            _e("ImagingStudy.series", T.BACKBONE, 0, "*"),
        ),
    ),
    "Consent": ResourceDefinition(
        resource_type="Consent",
        description="A patient's decision about disclosure of their data",
        search_parameters=("patient", "status", "category", "_id"),
        patient_element="patient",
        elements=(
            _e("Consent.id", T.ID, summary=True),
            _e("Consent.identifier", T.IDENTIFIER, 0, "*", summary=True),
            _e(
                "Consent.status",
                T.CODE,
                1,
                binding=frozenset(
                    {"draft", "active", "inactive", "entered-in-error", "unknown", "rejected"}
                ),
                summary=True,
            ),
            _e("Consent.category", T.CODEABLE_CONCEPT, 0, "*", summary=True),
            _e(
                "Consent.patient",
                T.REFERENCE,
                reference_targets=frozenset({"Patient"}),
                summary=True,
            ),
            _e("Consent.dateTime", T.DATETIME, versions=R4_ONLY, summary=True),
            _e("Consent.date", T.DATE, versions=R5_ONLY, summary=True),
            _e(
                "Consent.decision",
                T.CODE,
                binding=frozenset({"deny", "permit"}),
                versions=R5_ONLY,
                summary=True,
            ),
            _e("Consent.provision", T.BACKBONE, 0, "*"),
        ),
    ),
    "Appointment": ResourceDefinition(
        resource_type="Appointment",
        description="A scheduled encounter",
        search_parameters=("patient", "status", "date", "identifier", "_id"),
        patient_element="subject",
        elements=(
            _e("Appointment.id", T.ID, summary=True),
            _e("Appointment.identifier", T.IDENTIFIER, 0, "*", summary=True),
            _e(
                "Appointment.status",
                T.CODE,
                1,
                binding=frozenset(
                    {
                        "proposed",
                        "pending",
                        "booked",
                        "arrived",
                        "fulfilled",
                        "cancelled",
                        "noshow",
                        "entered-in-error",
                        "checked-in",
                        "waitlist",
                    }
                ),
                summary=True,
            ),
            _e("Appointment.start", T.INSTANT, summary=True),
            _e("Appointment.end", T.INSTANT, summary=True),
            _e("Appointment.participant", T.BACKBONE, 1, "*"),
            _e(
                "Appointment.subject",
                T.REFERENCE,
                reference_targets=frozenset({"Patient"}),
                versions=R5_ONLY,
                summary=True,
            ),
        ),
    ),
    "ChargeItem": ResourceDefinition(
        resource_type="ChargeItem",
        description="A billable item; the DFT target",
        search_parameters=("patient", "status", "code", "_id"),
        patient_element="subject",
        elements=(
            _e("ChargeItem.id", T.ID, summary=True),
            _e("ChargeItem.identifier", T.IDENTIFIER, 0, "*", summary=True),
            _e(
                "ChargeItem.status",
                T.CODE,
                1,
                binding=frozenset(
                    {
                        "planned",
                        "billable",
                        "not-billable",
                        "aborted",
                        "billed",
                        "entered-in-error",
                        "unknown",
                    }
                ),
                summary=True,
            ),
            _e("ChargeItem.code", T.CODEABLE_CONCEPT, 1, summary=True),
            _e(
                "ChargeItem.subject",
                T.REFERENCE,
                1,
                reference_targets=frozenset({"Patient"}),
                summary=True,
            ),
            _e("ChargeItem.occurrenceDateTime", T.DATETIME, choice_of="occurrence"),
            _e("ChargeItem.quantity", T.QUANTITY),
        ),
    ),
    "Task": ResourceDefinition(
        resource_type="Task",
        description="A unit of work; the closed-loop referral and order-tracking state machine",
        search_parameters=("patient", "status", "identifier", "_id"),
        patient_element="for",
        elements=(
            _e("Task.id", T.ID, summary=True),
            _e("Task.identifier", T.IDENTIFIER, 0, "*", summary=True),
            _e(
                "Task.status",
                T.CODE,
                1,
                binding=frozenset(
                    {
                        "draft",
                        "requested",
                        "received",
                        "accepted",
                        "rejected",
                        "ready",
                        "cancelled",
                        "in-progress",
                        "on-hold",
                        "failed",
                        "completed",
                        "entered-in-error",
                    }
                ),
                summary=True,
            ),
            _e(
                "Task.intent",
                T.CODE,
                1,
                binding=frozenset(
                    {
                        "unknown",
                        "proposal",
                        "plan",
                        "order",
                        "original-order",
                        "reflex-order",
                        "filler-order",
                        "instance-order",
                        "option",
                    }
                ),
                summary=True,
            ),
            _e(
                "Task.focus",
                T.REFERENCE,
                reference_targets=frozenset({"ServiceRequest", "MedicationRequest"}),
                summary=True,
            ),
            _e(
                "Task.for",
                T.REFERENCE,
                reference_targets=frozenset({"Patient"}),
                summary=True,
            ),
            _e(
                "Task.owner",
                T.REFERENCE,
                reference_targets=frozenset({"Practitioner", "Organization"}),
                summary=True,
            ),
            _e("Task.authoredOn", T.DATETIME),
            _e("Task.lastModified", T.DATETIME, summary=True),
            _e("Task.statusReason", T.CODEABLE_CONCEPT),
        ),
    ),
}


def definition_for(resource_type: str) -> ResourceDefinition | None:
    """The definition for a resource type, or ``None`` if this gateway does not serve it."""
    return _DEFINITIONS.get(resource_type)


def supported_resource_types() -> tuple[str, ...]:
    return tuple(sorted(_DEFINITIONS))
