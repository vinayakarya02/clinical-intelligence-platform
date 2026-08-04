"""Knowledge-graph retrieval.

Turns a natural-language query into graph evidence in three steps: find entry entities by
name, traverse outward under a hop budget, then render the discovered paths as candidates.

Graph candidates are *relationships*, not passages. A vector hit says "this text is about
your question"; a graph hit says "these two clinical concepts are connected, and here is the
document that established it". Downstream consumers treat them differently — graph evidence
is rendered as assertions with provenance rather than quoted as prose — which is why
:class:`SourceKind` distinguishes them.

Multi-hop is the whole point. "What interacts with this patient's medications?" is
unanswerable by similarity search: the answer is not textually similar to the question, it
is two edges away from it.
"""

from __future__ import annotations

from cip_core.logging import get_logger
from cip_retrieval.domain import (
    GraphEvidence,
    RetrievalCandidate,
    RetrievalQuery,
    RetrievalStrategy,
    SourceKind,
)
from cip_retrieval.graph.schema import NodeLabel
from cip_retrieval.graph.store import GraphStore
from cip_retrieval.graph.traversal import GraphPath, TraversalOptions, traverse

__all__ = ["GraphRetriever"]

_log = get_logger(__name__)

#: Labels worth using as traversal entry points. Excludes structural nodes (Encounter,
#: DocumentChunk) that match query text by accident rather than by clinical meaning.
_ENTRY_LABELS: tuple[NodeLabel, ...] = (
    NodeLabel.RXNORM_CONCEPT,
    NodeLabel.SNOMED_CONCEPT,
    NodeLabel.MEDICATION,
    NodeLabel.CONDITION,
    NodeLabel.SYMPTOM,
    NodeLabel.OBSERVATION,
    NodeLabel.PROCEDURE,
)


class GraphRetriever:
    """Finds entry entities, traverses, and returns paths as candidates."""

    def __init__(
        self,
        store: GraphStore,
        *,
        traversal: TraversalOptions | None = None,
        max_entry_points: int = 3,
    ) -> None:
        self._store = store
        self._traversal = traversal or TraversalOptions()
        self._max_entry_points = max_entry_points
        """Bounded because traversal cost is multiplied by entry-point count: a query
        matching a dozen entities would fan out into a dozen traversals."""

    @property
    def strategy(self) -> RetrievalStrategy:
        return RetrievalStrategy.GRAPH

    async def retrieve(self, query: RetrievalQuery, *, limit: int) -> list[RetrievalCandidate]:
        entry_nodes = await self._find_entry_points(query)
        if not entry_nodes:
            _log.debug("retrieval.graph_no_entry_points", query_length=len(query.text))
            return []

        seen: dict[str, GraphPath] = {}
        for label, key in entry_nodes:
            for path in await traverse(
                self._store,
                label=label,
                key=key,
                tenant_id=query.tenant_id,
                options=self._traversal,
            ):
                identity = f"{path.end_label}:{path.end_key}"
                # Two entry points can reach the same node by different routes; keep the
                # more confident (shorter, better-attested) path.
                existing = seen.get(identity)
                if existing is None or path.confidence > existing.confidence:
                    seen[identity] = path

        ranked = sorted(seen.values(), key=lambda path: path.confidence, reverse=True)[:limit]

        candidates: list[RetrievalCandidate] = []
        for rank, path in enumerate(ranked, start=1):
            candidate = RetrievalCandidate(
                id=f"graph:{path.end_label}:{path.end_key}",
                text=" ".join(path.as_sentences()),
                source_kind=SourceKind.GRAPH_PATH if path.hops > 1 else SourceKind.GRAPH_ENTITY,
                tenant_id=query.tenant_id,
                graph_evidence=tuple(self._to_evidence(path)),
                metadata={
                    "hops": path.hops,
                    "end_label": str(path.end_label),
                    "end_key": path.end_key,
                    "display_text": path.properties.get("display_text"),
                },
            )
            candidates.append(candidate.with_rank(RetrievalStrategy.GRAPH, rank, path.confidence))

        _log.debug(
            "retrieval.graph",
            entry_points=len(entry_nodes),
            returned=len(candidates),
        )
        return candidates

    async def _find_entry_points(self, query: RetrievalQuery) -> list[tuple[NodeLabel, str]]:
        """Locate entities named in the query text.

        Searches label by label rather than in one pass so a single very common label
        cannot consume the entire entry-point budget — otherwise a query mentioning one
        drug and one condition could return three drug nodes and no condition.
        """
        found: list[tuple[NodeLabel, str]] = []
        for label in _ENTRY_LABELS:
            nodes = await self._store.find_nodes(
                tenant_id=query.tenant_id, label=label, text=query.text, limit=2
            )
            found.extend((node.label, node.key) for node in nodes)
            if len(found) >= self._max_entry_points:
                break
        return found[: self._max_entry_points]

    @staticmethod
    def _to_evidence(path: GraphPath) -> list[GraphEvidence]:
        """Render a path's edges as attributable evidence."""
        evidence: list[GraphEvidence] = []
        current = path.start_key
        for edge in path.edges:
            subject, obj = (
                (current, edge.neighbour_key)
                if edge.direction == "outgoing"
                else (edge.neighbour_key, current)
            )
            evidence.append(
                GraphEvidence(
                    subject=subject,
                    predicate=str(edge.relationship_type),
                    object=obj,
                    confidence=edge.confidence,
                    evidence_level=edge.evidence_level,
                    source_document_id=edge.source_document_id,
                    hops=path.hops,
                )
            )
            current = edge.neighbour_key
        return evidence
