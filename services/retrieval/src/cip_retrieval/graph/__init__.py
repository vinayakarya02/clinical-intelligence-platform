"""Clinical knowledge graph.

``schema`` — labels, relationship types, and which are tenant-scoped or actionable.
``models`` — node/relationship value objects that enforce those rules at construction.
``store``  — Neo4j (production) and in-memory (development/CI) backends.
``traversal`` — bounded, confidence-decaying multi-hop walks.

Graph *retrieval* — turning a natural-language query into graph evidence — lives in
:mod:`cip_retrieval.retrievers.graph`, so this package stays about the graph itself.
"""

from cip_retrieval.graph.models import GraphNode, GraphRelationship, Provenance
from cip_retrieval.graph.schema import (
    ACTIONABLE_RELATIONSHIPS,
    ONTOLOGY_LABELS,
    PATIENT_SCOPED_LABELS,
    NodeLabel,
    RelationshipType,
    is_ontology_label,
    is_patient_scoped,
)
from cip_retrieval.graph.store import (
    GraphStore,
    InMemoryGraphStore,
    NeighbourEdge,
    Neo4jGraphStore,
)
from cip_retrieval.graph.traversal import GraphPath, TraversalOptions, traverse

__all__ = [
    "ACTIONABLE_RELATIONSHIPS",
    "ONTOLOGY_LABELS",
    "PATIENT_SCOPED_LABELS",
    "GraphNode",
    "GraphPath",
    "GraphRelationship",
    "GraphStore",
    "InMemoryGraphStore",
    "NeighbourEdge",
    "Neo4jGraphStore",
    "NodeLabel",
    "Provenance",
    "RelationshipType",
    "TraversalOptions",
    "is_ontology_label",
    "is_patient_scoped",
    "traverse",
]
