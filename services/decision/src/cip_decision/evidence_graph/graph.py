"""The evidence graph.

Records how a recommendation was produced, as a traversable path:

``Guideline → Evidence → Rule → Fact → Recommendation``

Extends the Phase 2 knowledge graph in shape rather than importing it — the decision service
must not depend on the retrieval service, and the node/edge model here is small enough that
sharing an implementation would couple two things that change for different reasons.

The graph is written at recommendation time and **kept**. That matters: a knowledge-base
upgrade that changes a rule must not change the recorded explanation of a recommendation made
under the old one, and a graph rebuilt on demand would do exactly that.
"""

from __future__ import annotations

import datetime as dt
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from cip_core.logging import get_logger
from cip_decision.domain import Recommendation

__all__ = ["EvidenceEdge", "EvidenceGraph", "EvidenceNode", "NodeKind"]

_log = get_logger(__name__)


class NodeKind(StrEnum):
    GUIDELINE = "guideline"
    EVIDENCE = "evidence"
    RULE = "rule"
    FACT = "fact"
    RECOMMENDATION = "recommendation"
    RISK_MODEL = "risk_model"
    PATHWAY = "pathway"
    DRUG_CHECK = "drug_check"
    MEDICATION = "medication"

    @property
    def is_terminal_subject(self) -> bool:
        """Whether this kind is a *subject* of a derivation rather than a step in it.

        Subjects attach directly to the recommendation. Chaining them made the graph claim
        one medication led to another, when in fact both are parallel inputs to the same
        finding.
        """
        return self in (NodeKind.FACT, NodeKind.MEDICATION)


@dataclass(frozen=True, slots=True)
class EvidenceNode:
    kind: NodeKind
    identifier: str
    label: str = ""
    attributes: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.kind.value}:{self.identifier}"


@dataclass(frozen=True, slots=True)
class EvidenceEdge:
    source: str
    target: str
    relation: str
    recorded_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))


class EvidenceGraph:
    """Provenance for produced recommendations."""

    def __init__(self, *, max_recommendations: int = 5000) -> None:
        self._nodes: dict[str, EvidenceNode] = {}
        self._edges: list[EvidenceEdge] = []
        self._incoming: dict[str, list[EvidenceEdge]] = {}
        self._recorded: OrderedDict[str, None] = OrderedDict()
        self._max_recommendations = max_recommendations
        """Recorded recommendations are evicted oldest-first past this bound.

        Unbounded, the graph grows several nodes and a dozen edges per recommendation
        forever — 200 patients produced 3,000 edges in testing — retaining clinical content
        for the life of the process. Production persists this; the bound is what makes the
        in-process implementation safe to run in a long-lived service."""

    def add_node(self, node: EvidenceNode) -> EvidenceNode:
        self._nodes.setdefault(node.key, node)
        return self._nodes[node.key]

    def connect(self, source: EvidenceNode, target: EvidenceNode, *, relation: str) -> None:
        self.add_node(source)
        self.add_node(target)
        edge = EvidenceEdge(source=source.key, target=target.key, relation=relation)
        self._edges.append(edge)
        self._incoming.setdefault(target.key, []).append(edge)

    def record(self, recommendation: Recommendation) -> EvidenceNode:
        """Write a recommendation's provenance chain into the graph.

        Built from the recommendation's own provenance links, which the constructor already
        guarantees are non-empty — so a recorded recommendation always has a traversable
        explanation.
        """
        target = self.add_node(
            EvidenceNode(
                kind=NodeKind.RECOMMENDATION,
                identifier=recommendation.id,
                label=recommendation.summary,
                attributes={
                    "severity": recommendation.severity.value,
                    "evidence_quality": recommendation.evidence_quality.value,
                },
            )
        )

        previous: EvidenceNode | None = None
        for link in recommendation.provenance:
            try:
                kind = NodeKind(link.kind)
            except ValueError:
                kind = NodeKind.EVIDENCE
            node = self.add_node(
                EvidenceNode(kind=kind, identifier=link.identifier, label=link.label)
            )

            if kind.is_terminal_subject:
                # A subject is a parallel input, not a step. Attaching it directly stops the
                # graph asserting that one medication led to another.
                self.connect(node, target, relation="subject_of")
                continue

            if previous is not None:
                self.connect(previous, node, relation="leads_to")
            previous = node

        if previous is not None:
            self.connect(previous, target, relation="produced")

        self._recorded[target.key] = None
        self._recorded.move_to_end(target.key)
        self._evict_oldest()

        for citation in recommendation.citations:
            evidence = self.add_node(
                EvidenceNode(
                    kind=NodeKind.EVIDENCE,
                    identifier=citation.render(),
                    label=citation.source,
                )
            )
            self.connect(evidence, target, relation="supports")

        return target

    def _evict_oldest(self) -> None:
        """Drop the oldest recorded recommendations and everything reaching only them."""
        while len(self._recorded) > self._max_recommendations:
            stale, _ = self._recorded.popitem(last=False)
            self._nodes.pop(stale, None)
            self._incoming.pop(stale, None)
            self._edges = [e for e in self._edges if e.target != stale and e.source != stale]

        # Sweep nodes no surviving edge references. A node kept alive by nothing is the same
        # leak in a different shape.
        referenced = {e.source for e in self._edges} | {e.target for e in self._edges}
        for key in [k for k in self._nodes if k not in referenced and k not in self._recorded]:
            del self._nodes[key]

    def explain(self, recommendation_id: str) -> tuple[str, ...]:
        """Every path that led to a recommendation, rendered.

        Walks backwards from the recommendation. Cycles are impossible by construction —
        provenance is a chain — but the visited set is kept anyway, because a graph that can
        be written by a future caller should not be able to hang a reader.
        """
        target = f"{NodeKind.RECOMMENDATION.value}:{recommendation_id}"
        if target not in self._nodes:
            return ()

        paths: list[str] = []

        def walk(node_key: str, trail: list[str], visited: set[str]) -> None:
            node = self._nodes[node_key]
            rendered = f"{node.kind.value}:{node.identifier}"
            trail = [rendered, *trail]

            incoming = [e for e in self._incoming.get(node_key, []) if e.source not in visited]
            if not incoming:
                paths.append(" → ".join(trail))
                return
            for edge in incoming:
                walk(edge.source, trail, visited | {node_key})

        walk(target, [], set())
        return tuple(sorted(set(paths)))

    def contribution(self, recommendation_id: str) -> dict[str, int]:
        """How many nodes of each kind contributed. Feeds the graph-utilisation metric."""
        counts: dict[str, int] = {}
        target = f"{NodeKind.RECOMMENDATION.value}:{recommendation_id}"
        seen: set[str] = set()
        frontier = [target]
        while frontier:
            key = frontier.pop()
            if key in seen:
                continue
            seen.add(key)
            node = self._nodes.get(key)
            if node is not None and key != target:
                counts[node.kind.value] = counts.get(node.kind.value, 0) + 1
            frontier.extend(e.source for e in self._incoming.get(key, []))
        return counts

    def node_count(self) -> int:
        return len(self._nodes)

    def edge_count(self) -> int:
        return len(self._edges)
