"""Graph storage: the Cypher backend and an in-process equivalent.

Two implementations, justified by the same reasoning as the vector store: Neo4j is not
available in CI or on every developer machine, and graph traversal logic that can only be
tested against a live server is graph traversal logic that goes untested.
:class:`InMemoryGraphStore` implements the same protocol with the same tenant-scoping rules,
so multi-hop traversal, provenance handling, and supersession are exercised for real.

The Cypher implementation uses ``MERGE`` for both nodes and relationships. Ingestion is
re-run routinely — after an OCR upgrade, a chunking change, a pipeline version bump — and
``CREATE`` would multiply every node and edge on each pass. ``MERGE`` on the natural key
makes re-ingestion idempotent, which is the difference between reprocessing a corpus and
corrupting it.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from cip_core.db.neo4j import Neo4jManager
from cip_core.logging import get_logger
from cip_retrieval.graph.models import GraphNode, GraphRelationship
from cip_retrieval.graph.schema import NodeLabel, RelationshipType, is_patient_scoped

__all__ = [
    "GraphStore",
    "InMemoryGraphStore",
    "NeighbourEdge",
    "Neo4jGraphStore",
    "lucene_query_for",
]

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class NeighbourEdge:
    """One hop away from a node, as returned by traversal."""

    relationship_type: RelationshipType
    neighbour_label: NodeLabel
    neighbour_key: str
    neighbour_properties: dict[str, Any] = field(default_factory=dict)
    confidence: float = 1.0
    source_document_id: uuid.UUID | None = None
    evidence_level: str | None = None
    direction: str = "outgoing"


@runtime_checkable
class GraphStore(Protocol):
    """Reads and writes the clinical knowledge graph."""

    @property
    def name(self) -> str: ...

    async def upsert_nodes(self, nodes: list[GraphNode]) -> int:
        """Merge nodes by natural key. Returns the number written."""
        ...

    async def upsert_relationships(self, relationships: list[GraphRelationship]) -> int:
        """Merge relationships. Returns the number written."""
        ...

    async def find_nodes(
        self,
        *,
        tenant_id: uuid.UUID | None,
        label: NodeLabel | None = None,
        text: str | None = None,
        limit: int = 25,
    ) -> list[GraphNode]:
        """Find nodes by label and/or display text."""
        ...

    async def neighbours(
        self,
        *,
        label: NodeLabel,
        key: str,
        tenant_id: uuid.UUID | None,
        relationship_types: tuple[RelationshipType, ...] = (),
        limit: int = 50,
    ) -> list[NeighbourEdge]:
        """Return edges one hop from a node."""
        ...

    async def health_check(self) -> dict[str, Any]: ...


_TEXT_TOKEN = re.compile(r"[a-z0-9]+")

#: Minimum token length worth using as an entity-matching signal. One- and two-character
#: tokens ("a", "of", "is", and every stray unit) match almost any node name and would turn
#: entity linking into a random sample of the graph.
_MIN_ENTITY_TOKEN = 3


def _tokens(text: str) -> frozenset[str]:
    """Lowercase alphanumeric tokens, for entity matching."""
    return frozenset(
        token for token in _TEXT_TOKEN.findall(text.lower()) if len(token) >= _MIN_ENTITY_TOKEN
    )


def lucene_query_for(text: str) -> str:
    """Make free text safe to pass to a Neo4j full-text (Lucene) index.

    ``db.index.fulltext.queryNodes`` takes a *Lucene query string*, not a literal. A clinical
    question is free text and routinely contains Lucene syntax — ``Na+/K+``, ``CT (chest)``,
    ``5.4 mmol/L``, a trailing ``?`` — and an unbalanced parenthesis or quote raises a parse
    error that takes the whole graph strategy down. Because the pipeline isolates retriever
    failures, that surfaces as *silently missing graph evidence* rather than an error, and
    only in production: the in-memory store tokenises instead of parsing, so CI never sees it.

    Reducing the question to its alphanumeric tokens and OR-joining them removes every
    reserved character by construction, and gives the same token-overlap semantics the
    in-memory store already has. Returns an empty string when nothing usable remains, and
    the caller must treat that as "no entity search" rather than passing it to Lucene.
    """
    tokens = _TEXT_TOKEN.findall(text.lower())
    meaningful = [token for token in tokens if len(token) >= _MIN_ENTITY_TOKEN]
    return " OR ".join(meaningful)


def _overlap(node: GraphNode, needle_tokens: frozenset[str]) -> int:
    """How many search tokens name this node.

    Token overlap, not substring containment. The search text is typically a whole
    natural-language question ("does lisinopril interact with spironolactone?"), and asking
    whether *that* string occurs inside the node name "Lisinopril" is backwards — it never
    matches, which silently disables graph retrieval for every multi-word query.

    Token matching also keeps this store consistent with Neo4jGraphStore, which uses a
    full-text index and therefore already has token semantics. A local store that behaves
    differently from production defeats the reason it exists. The *count* is returned rather
    than a boolean so matches can be ranked the way the full-text index ranks them.
    """
    if not needle_tokens:
        return 0
    display = str(node.properties.get("display_text", "")) + " " + node.key
    return len(_tokens(display) & needle_tokens)


class InMemoryGraphStore:
    """Adjacency-list graph for development and tests.

    Enforces the same tenant scoping as the Cypher backend, so an isolation bug fails here
    rather than only in production.
    """

    def __init__(self) -> None:
        self._nodes: dict[tuple[str, str, str | None], GraphNode] = {}
        self._edges: list[GraphRelationship] = []

    @property
    def name(self) -> str:
        return "memory"

    @staticmethod
    def _node_key(
        label: NodeLabel, key: str, tenant_id: uuid.UUID | None
    ) -> tuple[str, str, str | None]:
        return (str(label), key, str(tenant_id) if tenant_id else None)

    async def upsert_nodes(self, nodes: list[GraphNode]) -> int:
        for node in nodes:
            identity = self._node_key(node.label, node.key, node.tenant_id)
            existing = self._nodes.get(identity)
            if existing is not None:
                # Merge properties rather than replace: two extractions of the same
                # concept often carry different partial metadata, and last-write-wins
                # discards whichever arrived first.
                merged = {**existing.properties, **node.properties}
                self._nodes[identity] = GraphNode(
                    label=node.label,
                    key=node.key,
                    tenant_id=node.tenant_id,
                    properties=merged,
                    schema_version=node.schema_version,
                    valid_from=node.valid_from or existing.valid_from,
                    valid_to=node.valid_to or existing.valid_to,
                )
            else:
                self._nodes[identity] = node
        return len(nodes)

    async def upsert_relationships(self, relationships: list[GraphRelationship]) -> int:
        for relationship in relationships:
            duplicate = next(
                (
                    index
                    for index, existing in enumerate(self._edges)
                    if existing.type == relationship.type
                    and existing.start_key == relationship.start_key
                    and existing.end_key == relationship.end_key
                    and existing.start_label == relationship.start_label
                    and existing.end_label == relationship.end_label
                    and existing.tenant_id == relationship.tenant_id
                ),
                None,
            )
            if duplicate is None:
                self._edges.append(relationship)
            else:
                self._edges[duplicate] = relationship
        return len(relationships)

    async def find_nodes(
        self,
        *,
        tenant_id: uuid.UUID | None,
        label: NodeLabel | None = None,
        text: str | None = None,
        limit: int = 25,
    ) -> list[GraphNode]:
        needle = (text or "").strip()
        needle_tokens = _tokens(needle)
        if needle and not needle_tokens:
            # Search text was supplied but contains nothing usable ("is it ok?"). That is a
            # miss, not an unfiltered scan: falling through to "return everything" would
            # anchor graph traversal on arbitrary entities for a question that named none.
            return []

        scored: list[tuple[int, GraphNode]] = []

        for node in self._nodes.values():
            # A tenant-scoped node is visible only to its owner; a shared ontology node is
            # visible to everyone. Both rules matter: the first is isolation, the second is
            # what lets one drug-interaction fact serve every tenant.
            if node.tenant_id is not None and node.tenant_id != tenant_id:
                continue
            if label is not None and node.label != label:
                continue
            overlap = _overlap(node, needle_tokens)
            if needle_tokens and overlap == 0:
                continue
            scored.append((overlap, node))

        # Rank by overlap before truncating. Taking the first `limit` in insertion order
        # would make entry-point selection depend on write order, while Neo4j orders by
        # full-text score — so the local store would anchor traversal on different entities
        # than production and no test could see the difference.
        scored.sort(key=lambda pair: (-pair[0], str(pair[1].label), pair[1].key))
        return [node for _, node in scored[:limit]]

    async def neighbours(
        self,
        *,
        label: NodeLabel,
        key: str,
        tenant_id: uuid.UUID | None,
        relationship_types: tuple[RelationshipType, ...] = (),
        limit: int = 50,
    ) -> list[NeighbourEdge]:
        edges: list[NeighbourEdge] = []

        for edge in self._edges:
            if relationship_types and edge.type not in relationship_types:
                continue
            if edge.tenant_id is not None and edge.tenant_id != tenant_id:
                continue

            if edge.start_label == label and edge.start_key == key:
                other_label, other_key, direction = edge.end_label, edge.end_key, "outgoing"
            elif edge.end_label == label and edge.end_key == key:
                other_label, other_key, direction = edge.start_label, edge.start_key, "incoming"
            else:
                continue

            neighbour = self._nodes.get(self._node_key(other_label, other_key, edge.tenant_id))
            if neighbour is None:
                neighbour = self._nodes.get(self._node_key(other_label, other_key, None))

            edges.append(
                NeighbourEdge(
                    relationship_type=edge.type,
                    neighbour_label=other_label,
                    neighbour_key=other_key,
                    neighbour_properties=dict(neighbour.properties) if neighbour else {},
                    confidence=edge.confidence,
                    source_document_id=(
                        edge.provenance.source_document_id if edge.provenance else None
                    ),
                    evidence_level=edge.provenance.evidence_level if edge.provenance else None,
                    direction=direction,
                )
            )
            if len(edges) >= limit:
                break
        return edges

    async def health_check(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "backend": self.name,
            "nodes": len(self._nodes),
            "relationships": len(self._edges),
        }

    def clear(self) -> None:
        """Test helper; not part of the protocol."""
        self._nodes.clear()
        self._edges.clear()


class Neo4jGraphStore:
    """Cypher-backed graph store."""

    def __init__(self, manager: Neo4jManager) -> None:
        self._manager = manager

    @property
    def name(self) -> str:
        return "neo4j"

    async def upsert_nodes(self, nodes: list[GraphNode]) -> int:
        """Merge nodes in one batched transaction per label.

        Batched by label because Cypher cannot parameterise a label — it is part of the
        query text, not a parameter — so a mixed batch would need string interpolation per
        row. Grouping keeps the label out of the interpolated portion and the data fully
        parameterised, which is also what keeps this injection-safe.
        """
        if not nodes:
            return 0

        by_label: dict[NodeLabel, list[GraphNode]] = {}
        for node in nodes:
            by_label.setdefault(node.label, []).append(node)

        written = 0
        async with self._manager.write_session() as session:
            for label, group in by_label.items():
                # From the schema, not from `group[0].tenant_id`: reading the scoping rule
                # off a data sample means one mislabelled node silently changes the MERGE
                # key for the whole batch, collapsing every tenant's nodes onto one.
                match_clause = (
                    "{key: row.key, tenant_id: row.tenant_id}"
                    if is_patient_scoped(label)
                    else "{key: row.key}"
                )
                query = (
                    "UNWIND $rows AS row "
                    f"MERGE (n:{label.value} {match_clause}) "
                    "SET n += row.properties, "
                    "    n.schema_version = row.schema_version, "
                    "    n.valid_from = row.valid_from, "
                    "    n.valid_to = row.valid_to "
                    "RETURN count(n) AS written"
                )
                result = await session.run(
                    query,
                    rows=[
                        {
                            "key": node.key,
                            "tenant_id": str(node.tenant_id) if node.tenant_id else None,
                            "properties": node.properties,
                            "schema_version": node.schema_version,
                            "valid_from": node.valid_from.isoformat() if node.valid_from else None,
                            "valid_to": node.valid_to.isoformat() if node.valid_to else None,
                        }
                        for node in group
                    ],
                )
                record = await result.single()
                written += int(record["written"]) if record else 0

        _log.debug("graph.nodes_upserted", backend=self.name, count=written)
        return written

    async def upsert_relationships(self, relationships: list[GraphRelationship]) -> int:
        if not relationships:
            return 0

        grouped: dict[tuple[RelationshipType, NodeLabel, NodeLabel], list[GraphRelationship]] = {}
        for relationship in relationships:
            grouped.setdefault(
                (relationship.type, relationship.start_label, relationship.end_label), []
            ).append(relationship)

        written = 0
        async with self._manager.write_session() as session:
            for (rel_type, start_label, end_label), group in grouped.items():
                # Endpoint MATCHes must be tenant-scoped for patient-scoped labels. `key` is
                # unique only *within* a tenant — two tenants can both hold a Patient keyed
                # by the same MRN — so matching on key alone matches every tenant's node and
                # MERGE then writes one tenant's clinical assertion onto another tenant's
                # patient. Ontology labels are shared and correctly match on key alone.
                start_match = (
                    "{key: row.start_key, tenant_id: row.tenant_id}"
                    if is_patient_scoped(start_label)
                    else "{key: row.start_key}"
                )
                end_match = (
                    "{key: row.end_key, tenant_id: row.tenant_id}"
                    if is_patient_scoped(end_label)
                    else "{key: row.end_key}"
                )
                query = (
                    "UNWIND $rows AS row "
                    f"MATCH (a:{start_label.value} {start_match}) "
                    f"MATCH (b:{end_label.value} {end_match}) "
                    f"MERGE (a)-[r:{rel_type.value} {{tenant_id: row.tenant_id}}]->(b) "
                    "SET r += row.properties, r.confidence = row.confidence "
                    "RETURN count(r) AS written"
                )
                rows = []
                for relationship in group:
                    properties = dict(relationship.properties)
                    if relationship.provenance is not None:
                        properties.update(relationship.provenance.as_properties())
                    rows.append(
                        {
                            "start_key": relationship.start_key,
                            "end_key": relationship.end_key,
                            # Written into the MERGE key, not just as a property: an edge's
                            # tenant is part of its identity, and `neighbours` filters on it.
                            # Dropping it here made every tenant-scoped edge readable by
                            # every tenant, while the in-memory store filtered correctly and
                            # hid the divergence from CI.
                            "tenant_id": (
                                str(relationship.tenant_id) if relationship.tenant_id else None
                            ),
                            "confidence": relationship.confidence,
                            "properties": properties,
                        }
                    )
                result = await session.run(query, rows=rows)
                record = await result.single()
                written += int(record["written"]) if record else 0

        if written < len(relationships):
            # A row whose endpoints did not MATCH is dropped by Cypher without error. For a
            # clinically actionable edge that is a silent loss of evidence, so it is logged
            # rather than left for the caller to notice by comparing counts.
            _log.warning(
                "graph.relationships_partially_written",
                backend=self.name,
                requested=len(relationships),
                written=written,
                reason="endpoint nodes not found or not visible to the edge's tenant",
            )

        _log.debug("graph.relationships_upserted", backend=self.name, count=written)
        return written

    async def find_nodes(
        self,
        *,
        tenant_id: uuid.UUID | None,
        label: NodeLabel | None = None,
        text: str | None = None,
        limit: int = 25,
    ) -> list[GraphNode]:
        """Find nodes by label and display text.

        Uses the full-text index rather than a ``CONTAINS`` scan. Neo4j full-text queries
        cannot carry an inline tenant predicate, so the tenant filter is applied in the
        ``WHERE`` clause immediately after — mandatory, not optional: in shared-database
        mode a missed post-filter here is a cross-tenant PHI leak
        (docs/database/graph-schema.md §7).
        """
        label_filter = f":{label.value}" if label is not None else ""
        tenant_value = str(tenant_id) if tenant_id else None
        search_text = lucene_query_for(text) if text else ""
        if text and text.strip() and not search_text:
            # Same rule as the in-memory store: a question with no usable tokens matches no
            # entity. Falling through to the label scan below would return arbitrary nodes.
            return []

        if search_text:
            query = (
                "CALL db.index.fulltext.queryNodes('entity_display_text', $text) "
                "YIELD node, score "
                "WHERE (node.tenant_id IS NULL OR node.tenant_id = $tenant_id) "
                + (f"AND node:{label.value} " if label is not None else "")
                + "RETURN node, labels(node) AS labels ORDER BY score DESC LIMIT $limit"
            )
            params: dict[str, Any] = {
                "text": search_text,
                "tenant_id": tenant_value,
                "limit": limit,
            }
        else:
            query = (
                f"MATCH (node{label_filter}) "
                "WHERE (node.tenant_id IS NULL OR node.tenant_id = $tenant_id) "
                "RETURN node, labels(node) AS labels LIMIT $limit"
            )
            params = {"tenant_id": tenant_value, "limit": limit}

        async with self._manager.read_session() as session:
            result = await session.run(query, **params)
            records = [record async for record in result]

        return [self._to_node(record["node"], record["labels"]) for record in records]

    async def neighbours(
        self,
        *,
        label: NodeLabel,
        key: str,
        tenant_id: uuid.UUID | None,
        relationship_types: tuple[RelationshipType, ...] = (),
        limit: int = 50,
    ) -> list[NeighbourEdge]:
        """Return edges one hop from a node.

        The hop count is fixed at one and the caller composes multi-hop traversal
        explicitly (:mod:`cip_retrieval.graph.traversal`). An unbounded variable-length
        pattern here would be an open invitation to a runaway query — transitively
        traversable edges like ``INCREASES_RISK_OF`` expand exponentially.
        """
        type_filter = ""
        if relationship_types:
            type_filter = ":" + "|".join(rel.value for rel in relationship_types)

        query = (
            f"MATCH (n:{label.value} {{key: $key}})-[r{type_filter}]-(m) "
            "WHERE (n.tenant_id IS NULL OR n.tenant_id = $tenant_id) "
            "  AND (m.tenant_id IS NULL OR m.tenant_id = $tenant_id) "
            # The edge carries its own tenant. Filtering only the endpoints is not enough:
            # a tenant-scoped assertion drawn between two *shared* ontology nodes has two
            # null-tenant endpoints, so an endpoint-only filter exposes it to every tenant.
            "  AND (r.tenant_id IS NULL OR r.tenant_id = $tenant_id) "
            "RETURN type(r) AS rel_type, r AS rel, m AS neighbour, labels(m) AS labels, "
            "       startNode(r) = n AS is_outgoing "
            "LIMIT $limit"
        )
        async with self._manager.read_session() as session:
            result = await session.run(
                query,
                key=key,
                tenant_id=str(tenant_id) if tenant_id else None,
                limit=limit,
            )
            records = [record async for record in result]

        edges: list[NeighbourEdge] = []
        for record in records:
            rel = dict(record["rel"])
            raw_source = rel.get("source_document_id")
            edges.append(
                NeighbourEdge(
                    relationship_type=RelationshipType(record["rel_type"]),
                    neighbour_label=self._primary_label(record["labels"]),
                    neighbour_key=dict(record["neighbour"]).get("key", ""),
                    neighbour_properties=dict(record["neighbour"]),
                    confidence=float(rel.get("confidence", 1.0)),
                    source_document_id=uuid.UUID(raw_source) if raw_source else None,
                    evidence_level=rel.get("evidence_level"),
                    direction="outgoing" if record["is_outgoing"] else "incoming",
                )
            )
        return edges

    @staticmethod
    def _primary_label(labels: list[str]) -> NodeLabel:
        for value in labels:
            try:
                return NodeLabel(value)
            except ValueError:
                continue
        return NodeLabel.LOCAL_CONCEPT

    def _to_node(self, raw: Any, labels: list[str]) -> GraphNode:
        properties = dict(raw)
        tenant_raw = properties.pop("tenant_id", None)
        return GraphNode(
            label=self._primary_label(labels),
            key=properties.pop("key", ""),
            tenant_id=uuid.UUID(tenant_raw) if tenant_raw else None,
            properties=properties,
        )

    async def health_check(self) -> dict[str, Any]:
        health = await self._manager.health_check()
        return {"status": health.get("status", "unknown"), "backend": self.name}
