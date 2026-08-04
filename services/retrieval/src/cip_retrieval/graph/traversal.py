"""Multi-hop graph traversal.

Traversal is composed here from single-hop store calls rather than expressed as a
variable-length Cypher pattern, for one reason: **bounded cost**. Transitively traversable
clinical edges (``INCREASES_RISK_OF``, ``CONTRAINDICATED_WITH``) fan out exponentially, and
an unbounded pattern against a hospital-scale graph is a query that never returns. Composing
hops explicitly makes the depth limit, the per-level breadth limit, and the total node
budget all enforceable and testable.

Confidence decays with distance. A two-hop inference ("drug A interacts with drug B, which
treats condition C") is genuinely weaker evidence than the one-hop facts it chains, and a
traversal that reported both at full confidence would let speculative multi-hop paths
outrank direct evidence during reranking.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from cip_core.logging import get_logger
from cip_retrieval.graph.schema import NodeLabel, RelationshipType
from cip_retrieval.graph.store import GraphStore, NeighbourEdge

__all__ = ["GraphPath", "TraversalOptions", "traverse"]

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class TraversalOptions:
    """Bounds on a traversal. Every one of these exists to cap a real cost."""

    max_hops: int = 2
    """Clinical multi-hop questions are nearly always answerable within two hops; beyond
    that, precision collapses faster than recall improves."""

    max_neighbours_per_node: int = 25
    """Breadth cap. A hub node — a common drug appearing in thousands of records — would
    otherwise flood the frontier and starve every other branch."""

    max_total_nodes: int = 200
    """Hard budget across the whole traversal, independent of depth and breadth."""

    relationship_types: tuple[RelationshipType, ...] = ()
    min_confidence: float = 0.0
    hop_decay: float = 0.6
    """Per-hop confidence multiplier. 0.6 makes a two-hop path 36% as trusted as a
    one-hop fact, which keeps chained inferences from outranking direct evidence."""

    def __post_init__(self) -> None:
        if self.max_hops < 1:
            raise ValueError("max_hops must be >= 1")
        if not 0.0 < self.hop_decay <= 1.0:
            raise ValueError("hop_decay must be in (0, 1]")


@dataclass(frozen=True, slots=True)
class GraphPath:
    """A path from a starting node to a discovered node."""

    start_label: NodeLabel
    start_key: str
    end_label: NodeLabel
    end_key: str
    edges: tuple[NeighbourEdge, ...]
    confidence: float
    """Product of edge confidences and per-hop decay."""
    properties: dict[str, object] = field(default_factory=dict)

    @property
    def hops(self) -> int:
        return len(self.edges)

    def as_sentences(self) -> list[str]:
        """Render each edge as readable text for context assembly."""
        sentences: list[str] = []
        current = self.start_key
        for edge in self.edges:
            predicate = edge.relationship_type.value.replace("_", " ").lower()
            subject, obj = (
                (current, edge.neighbour_key)
                if edge.direction == "outgoing"
                else (edge.neighbour_key, current)
            )
            sentences.append(f"{subject} {predicate} {obj}")
            current = edge.neighbour_key
        return sentences


async def traverse(
    store: GraphStore,
    *,
    label: NodeLabel,
    key: str,
    tenant_id: uuid.UUID | None,
    options: TraversalOptions | None = None,
) -> list[GraphPath]:
    """Breadth-first traversal from one node, returning discovered paths.

    Breadth-first rather than depth-first so the closest — and therefore most trustworthy —
    evidence is found before the node budget is spent. A depth-first walk would exhaust the
    budget down one speculative branch while a directly relevant one-hop fact sat
    undiscovered.

    A node is visited once, at its shortest path. Re-expanding it via a longer route only
    produces weaker duplicates of paths already found.
    """
    options = options or TraversalOptions()

    visited: set[tuple[str, str]] = {(str(label), key)}
    frontier: list[tuple[NodeLabel, str, tuple[NeighbourEdge, ...], float]] = [
        (label, key, (), 1.0)
    ]
    paths: list[GraphPath] = []

    for hop in range(1, options.max_hops + 1):
        next_frontier: list[tuple[NodeLabel, str, tuple[NeighbourEdge, ...], float]] = []

        for current_label, current_key, edges_so_far, confidence in frontier:
            if len(visited) >= options.max_total_nodes:
                _log.debug("graph.traversal_budget_exhausted", hop=hop, visited=len(visited))
                break

            neighbours = await store.neighbours(
                label=current_label,
                key=current_key,
                tenant_id=tenant_id,
                relationship_types=options.relationship_types,
                limit=options.max_neighbours_per_node,
            )

            for edge in neighbours:
                identity = (str(edge.neighbour_label), edge.neighbour_key)
                if identity in visited:
                    continue

                path_confidence = confidence * edge.confidence * options.hop_decay
                if path_confidence < options.min_confidence:
                    continue

                visited.add(identity)
                path_edges = (*edges_so_far, edge)
                paths.append(
                    GraphPath(
                        start_label=label,
                        start_key=key,
                        end_label=edge.neighbour_label,
                        end_key=edge.neighbour_key,
                        edges=path_edges,
                        confidence=path_confidence,
                        properties=dict(edge.neighbour_properties),
                    )
                )

                if hop < options.max_hops:
                    next_frontier.append(
                        (edge.neighbour_label, edge.neighbour_key, path_edges, path_confidence)
                    )

                if len(visited) >= options.max_total_nodes:
                    break

        frontier = next_frontier
        if not frontier:
            break

    paths.sort(key=lambda path: path.confidence, reverse=True)
    return paths
