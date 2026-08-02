"""Domain enumerations shared by the ORM models, API schemas, and pipeline.

These are the single source of truth for values that also appear as ``CHECK`` constraints
in docs/database/postgres-schema.sql. Defining them once and generating the constraint
from the enum keeps the database and the application from drifting apart — a drift that
otherwise surfaces as an ``IntegrityError`` in production rather than a test failure.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "DeidentificationStatus",
    "DocumentType",
    "IngestionStatus",
    "PipelineStage",
    "QualityVerdict",
    "SectionType",
    "SyncStatus",
    "SyncTarget",
]


class DocumentType(StrEnum):
    """Document classes the platform ingests (docs/api/openapi.yaml ``documentType``)."""

    DISCHARGE_SUMMARY = "discharge_summary"
    LAB_REPORT = "lab_report"
    RADIOLOGY_NOTE = "radiology_note"
    TRIAL_PROTOCOL = "trial_protocol"
    ADVERSE_EVENT_REPORT = "adverse_event_report"
    LITERATURE = "literature"
    GUIDELINE = "guideline"
    HL7V2_MESSAGE = "hl7v2_message"
    FHIR_BUNDLE = "fhir_bundle"
    DICOM_STUDY = "dicom_study"
    UNKNOWN = "unknown"
    """Assigned when classification confidence is too low to commit to a type. Retained
    as an explicit value rather than defaulting to a plausible-looking type, so downstream
    consumers can tell "we do not know" from "we decided"."""

    @property
    def is_narrative(self) -> bool:
        """Whether this type carries narrative prose that should be chunked and embedded.

        Structured clinical interchange formats bypass chunk-embedding entirely and
        populate the relational/graph stores directly
        (docs/architecture/02-rag-hybrid-retrieval.md §1.1).
        """
        return self not in {
            DocumentType.HL7V2_MESSAGE,
            DocumentType.FHIR_BUNDLE,
            DocumentType.DICOM_STUDY,
        }


class IngestionStatus(StrEnum):
    """Lifecycle of a document through the ingestion pipeline.

    Phase 1 terminates at ``EMBEDDED``'s predecessor: chunking completes and chunks are
    persisted, but embedding is Phase 2. ``CHUNKED`` is therefore the Phase 1 success
    terminal state, and the later states exist so the enum does not need a breaking
    change when Phase 2 lands.
    """

    PENDING = "pending"
    PARSED = "parsed"
    NORMALIZED = "normalized"
    CHUNKED = "chunked"
    EMBEDDED = "embedded"
    GRAPH_INDEXED = "graph_indexed"
    FAILED = "failed"
    QUARANTINED = "quarantined"
    """Ingested and stored, but failed data-quality gating — deliberately not ``FAILED``:
    the bytes are safely persisted and the document is auditable and re-processable, it
    is simply withheld from retrieval until a human reviews it."""

    @property
    def is_terminal(self) -> bool:
        return self in {
            IngestionStatus.GRAPH_INDEXED,
            IngestionStatus.FAILED,
            IngestionStatus.QUARANTINED,
        }


class DeidentificationStatus(StrEnum):
    """HIPAA de-identification state (docs/architecture/06-security-compliance.md §4)."""

    NOT_DEIDENTIFIED = "not_deidentified"
    SAFE_HARBOR = "safe_harbor"
    EXPERT_DETERMINATION = "expert_determination"


class SectionType(StrEnum):
    """Canonical chunk section classification.

    ``GENERATED_*`` values mark chunks synthesised from structured data rather than
    extracted prose. Retrieval must be able to tell them apart because a generated
    summary is retrieval scaffolding, never a citable source of truth
    (docs/architecture/02-rag-hybrid-retrieval.md §1.2).
    """

    NARRATIVE = "narrative"
    PROBLEM_LIST = "problem_list"
    GENERATED_MEDICATION_SUMMARY = "generated_medication_summary"
    GENERATED_LAB_SUMMARY = "generated_lab_summary"
    OTHER = "other"


class PipelineStage(StrEnum):
    """ETL stages, in execution order. Used for per-stage status and error attribution."""

    VALIDATE = "validate"
    DEDUPLICATE = "deduplicate"
    PERSIST_RAW = "persist_raw"
    PARSE = "parse"
    NORMALIZE = "normalize"
    DETECT_SECTIONS = "detect_sections"
    EXTRACT_METADATA = "extract_metadata"
    CHUNK = "chunk"
    QUALITY_CHECK = "quality_check"
    PERSIST_ARTIFACTS = "persist_artifacts"


class QualityVerdict(StrEnum):
    """Outcome of data-quality gating."""

    PASS = "pass"
    WARN = "warn"
    """Below preferred thresholds but usable; the document proceeds and the report records why."""
    FAIL = "fail"
    """Below the minimum usable threshold; the document is quarantined, not discarded."""


class SyncTarget(StrEnum):
    """Downstream index a document must be propagated to."""

    OPENSEARCH = "opensearch"
    NEO4J = "neo4j"
    VECTOR = "vector"


class SyncStatus(StrEnum):
    PENDING = "pending"
    SYNCED = "synced"
    FAILED = "failed"
    STALE = "stale"
