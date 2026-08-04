"""Graph node and relationship value objects, with their validation rules.

The validation here is the enforcement point for the schema's design rules — it is where
"provenance is mandatory on actionable edges" stops being documentation and becomes a
constraint. Enforcing it in the value object rather than in the writer means every path
into the graph is covered, including future ones.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field
from typing import Any

from cip_retrieval.graph.schema import (
    ACTIONABLE_RELATIONSHIPS,
    NodeLabel,
    RelationshipType,
    is_patient_scoped,
)

__all__ = ["GraphNode", "GraphRelationship", "Provenance"]


@dataclass(frozen=True, slots=True)
class Provenance:
    """Where an assertion came from.

    ``asserted_by`` distinguishes an extraction pipeline from a curated import from a
    clinician's entry — the same claim carries different weight depending on its origin,
    and reranking uses that difference.
    """

    source_document_id: uuid.UUID | None = None
    source_chunk_id: str | None = None
    asserted_by: str = "extraction_pipeline"
    evidence_level: str | None = None
    asserted_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))

    def as_properties(self, prefix: str = "") -> dict[str, Any]:
        """Flatten to Cypher-settable properties.

        Flat rather than nested because Neo4j properties cannot hold maps; a nested dict
        would be rejected at write time.
        """
        return {
            f"{prefix}source_document_id": (
                str(self.source_document_id) if self.source_document_id else None
            ),
            f"{prefix}source_chunk_id": self.source_chunk_id,
            f"{prefix}asserted_by": self.asserted_by,
            f"{prefix}evidence_level": self.evidence_level,
            f"{prefix}asserted_at": self.asserted_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class GraphNode:
    """A node awaiting write.

    ``key`` is the natural identifier within the label — a patient id, a SNOMED concept id,
    an RxCUI. Merges are keyed on ``(label, key)`` for ontology nodes and
    ``(label, tenant_id, key)`` for patient-scoped ones, which is what makes ingestion
    idempotent: re-processing a document updates nodes rather than duplicating them.
    """

    label: NodeLabel
    key: str
    tenant_id: uuid.UUID | None = None
    properties: dict[str, Any] = field(default_factory=dict)
    schema_version: int = 1
    valid_from: dt.date | None = None
    valid_to: dt.date | None = None
    """Clinical validity (when the fact was true of the patient), distinct from
    ``asserted_at`` (when the platform recorded it). Both are needed for point-in-time
    queries — see docs/database/graph-schema.md §3."""

    def __post_init__(self) -> None:
        if not self.key.strip():
            raise ValueError("GraphNode.key must not be empty")
        if is_patient_scoped(self.label) and self.tenant_id is None:
            raise ValueError(
                f"{self.label} is patient-scoped and requires a tenant_id — an unscoped "
                "patient node is a cross-tenant leak waiting to happen"
            )
        if not is_patient_scoped(self.label) and self.tenant_id is not None:
            raise ValueError(
                f"{self.label} is shared reference data and must not carry a tenant_id — "
                "duplicating ontology per tenant defeats the shared concept layer"
            )

    @property
    def merge_key(self) -> dict[str, Any]:
        """The properties a ``MERGE`` matches on."""
        if self.tenant_id is not None:
            return {"key": self.key, "tenant_id": str(self.tenant_id)}
        return {"key": self.key}


@dataclass(frozen=True, slots=True)
class GraphRelationship:
    """An edge awaiting write."""

    type: RelationshipType
    start_label: NodeLabel
    start_key: str
    end_label: NodeLabel
    end_key: str
    tenant_id: uuid.UUID | None = None
    confidence: float = 1.0
    provenance: Provenance | None = None
    properties: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be in [0, 1]")
        # The rule from docs/database/graph-schema.md §0: an actionable edge with no
        # attributable source cannot be reviewed, defended, or safely surfaced to a
        # clinician. Rejecting it here covers every write path.
        if self.type in ACTIONABLE_RELATIONSHIPS and (
            self.provenance is None
            or (
                self.provenance.source_document_id is None
                and self.provenance.source_chunk_id is None
            )
        ):
            raise ValueError(
                f"{self.type} is clinically actionable and requires provenance "
                "(a source document or chunk)"
            )
