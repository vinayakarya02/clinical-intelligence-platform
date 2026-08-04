"""Clinical knowledge graph schema.

Implements the model designed in docs/database/graph-schema.md. The design decisions that
shape this module, restated because they constrain every function below:

**Two layers.** Patient-instance nodes (this patient's diagnosis) are tenant-scoped;
ontology and shared-knowledge nodes (the SNOMED concept, the drug interaction) are
tenant-agnostic reference data. Generic pharmacological knowledge lives on the ontology
layer so it is stated once, not duplicated per patient.

**Provenance is mandatory on clinically actionable edges.** A contraindication with no
source document is not usable in a clinical setting — the reviewer cannot tell an FDA label
from a hallucinated extraction. :data:`ACTIONABLE_RELATIONSHIPS` names the edges that
require it and :func:`validate_relationship` enforces it.

**Versioning is by supersession, not mutation.** Facts are appended and linked with
``SUPERSEDES`` rather than overwritten, so a point-in-time query can recover what was
believed on any past date.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "ACTIONABLE_RELATIONSHIPS",
    "ONTOLOGY_LABELS",
    "PATIENT_SCOPED_LABELS",
    "NodeLabel",
    "RelationshipType",
    "is_ontology_label",
    "is_patient_scoped",
]


class NodeLabel(StrEnum):
    """Node labels. Mirrors docs/database/graph-schema.md §1."""

    # Patient-instance layer — tenant-scoped.
    PATIENT = "Patient"
    ENCOUNTER = "Encounter"
    CONDITION = "Condition"
    """A diagnosis or clinical finding recorded for a patient."""
    MEDICATION = "Medication"
    SYMPTOM = "Symptom"
    OBSERVATION = "Observation"
    """A lab or vital sign result."""
    PROCEDURE = "Procedure"
    ALLERGY_INTOLERANCE = "AllergyIntolerance"
    DEVICE = "Device"
    PROVIDER = "Provider"
    ORGANIZATION = "Organization"
    DOCUMENT_CHUNK = "DocumentChunk"

    # Ontology layer — tenant-agnostic reference data.
    UMLS_CONCEPT = "UmlsConcept"
    SNOMED_CONCEPT = "SnomedConcept"
    ICD_CODE = "IcdCode"
    LOINC_CODE = "LoincCode"
    RXNORM_CONCEPT = "RxNormConcept"
    HPO_TERM = "HpoTerm"
    LOCAL_CONCEPT = "LocalConcept"
    """Fallback for entities that resolve to no ontology concept. Real extraction never
    achieves full coverage, and dropping the remainder loses clinical content silently."""

    # Shared clinical knowledge — tenant-agnostic.
    CLINICAL_TRIAL = "ClinicalTrial"
    ADVERSE_EVENT_DEFINITION = "AdverseEventDefinition"
    GUIDELINE = "Guideline"
    LITERATURE_SOURCE = "LiteratureSource"


class RelationshipType(StrEnum):
    """Relationship types. Specific by design — a generic ``RELATED_TO`` would destroy the
    multi-hop reasoning value the graph exists to provide."""

    # Patient-instance structure.
    HAS_ENCOUNTER = "HAS_ENCOUNTER"
    DIAGNOSED_WITH = "DIAGNOSED_WITH"
    PRESCRIBED = "PRESCRIBED"
    PERFORMED = "PERFORMED"
    HAS_OBSERVATION = "HAS_OBSERVATION"
    HAS_SYMPTOM = "HAS_SYMPTOM"
    HAS_ALLERGY = "HAS_ALLERGY"
    ATTENDED_BY = "ATTENDED_BY"

    # Ontology resolution.
    HAS_SNOMED_CONCEPT = "HAS_SNOMED_CONCEPT"
    HAS_RXNORM_CONCEPT = "HAS_RXNORM_CONCEPT"
    HAS_LOINC_CODE = "HAS_LOINC_CODE"
    HAS_LOCAL_CONCEPT = "HAS_LOCAL_CONCEPT"
    HAS_CUI = "HAS_CUI"
    MAPPED_TO = "MAPPED_TO"

    # Clinical knowledge — asserted between ontology concepts, not patient instances.
    TREATS = "TREATS"
    CONTRAINDICATED_WITH = "CONTRAINDICATED_WITH"
    CAUSES = "CAUSES"
    INCREASES_RISK_OF = "INCREASES_RISK_OF"
    INTERACTS_WITH = "INTERACTS_WITH"

    # Evidence and provenance.
    MENTIONS = "MENTIONS"
    RECOMMENDS = "RECOMMENDS"
    REPORTED = "REPORTED"
    STUDIES = "STUDIES"
    CITES = "CITES"
    SUPERSEDES = "SUPERSEDES"


#: Labels whose nodes belong to exactly one tenant. Every one of these must carry
#: ``tenant_id`` and every query touching them must filter on it.
PATIENT_SCOPED_LABELS: frozenset[NodeLabel] = frozenset(
    {
        NodeLabel.PATIENT,
        NodeLabel.ENCOUNTER,
        NodeLabel.CONDITION,
        NodeLabel.MEDICATION,
        NodeLabel.SYMPTOM,
        NodeLabel.OBSERVATION,
        NodeLabel.PROCEDURE,
        NodeLabel.ALLERGY_INTOLERANCE,
        NodeLabel.DEVICE,
        NodeLabel.PROVIDER,
        NodeLabel.ORGANIZATION,
        NodeLabel.DOCUMENT_CHUNK,
        NodeLabel.LOCAL_CONCEPT,
    }
)

#: Tenant-agnostic reference data, shared across all tenants and never duplicated per
#: tenant. A SNOMED concept means the same thing for everyone.
ONTOLOGY_LABELS: frozenset[NodeLabel] = frozenset(
    {
        NodeLabel.UMLS_CONCEPT,
        NodeLabel.SNOMED_CONCEPT,
        NodeLabel.ICD_CODE,
        NodeLabel.LOINC_CODE,
        NodeLabel.RXNORM_CONCEPT,
        NodeLabel.HPO_TERM,
        NodeLabel.CLINICAL_TRIAL,
        NodeLabel.ADVERSE_EVENT_DEFINITION,
        NodeLabel.GUIDELINE,
        NodeLabel.LITERATURE_SOURCE,
    }
)

#: Edges that can drive a clinical decision. Each requires provenance and a confidence
#: score: a contraindication with no attributable source cannot be acted on or defended.
ACTIONABLE_RELATIONSHIPS: frozenset[RelationshipType] = frozenset(
    {
        RelationshipType.TREATS,
        RelationshipType.CONTRAINDICATED_WITH,
        RelationshipType.CAUSES,
        RelationshipType.INCREASES_RISK_OF,
        RelationshipType.INTERACTS_WITH,
        RelationshipType.RECOMMENDS,
    }
)


def is_patient_scoped(label: NodeLabel) -> bool:
    """Whether nodes with this label must carry a ``tenant_id``."""
    return label in PATIENT_SCOPED_LABELS


def is_ontology_label(label: NodeLabel) -> bool:
    """Whether this label is shared reference data rather than tenant-owned."""
    return label in ONTOLOGY_LABELS
