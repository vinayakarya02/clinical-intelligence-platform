"""Explanation assembly.

FDA's January 2026 clinical-decision-support guidance keeps its evaluation focused on whether
the healthcare professional can understand the *basis* of a recommendation. That is the
requirement this module exists to satisfy, and it is why explanation is a pipeline stage
rather than a rendering option: an answer without one is not shippable.

An explanation has four parts, and each answers a question a reviewing clinician actually
asks:

- **Evidence** — what did you read? (cited, with provenance)
- **Graph path** — why did the knowledge graph matter here? (narrated as a chain, not a node list)
- **Reasoning trace** — what did you do, in order, and why?
- **Confidence** — how sure are you, decomposed, and what is the weakest part?

The graph narration is the piece that distinguishes this from a citation list. Retrieving
three nodes explains nothing; saying "the guideline applies *because* the diagnosis leads to
this medication, which interacts with that one" is an explanation.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from cip_copilot.domain import (
    Claim,
    ConfidenceBreakdown,
    Evidence,
    EvidenceKind,
    StageRecord,
)

__all__ = ["Explanation", "GraphNarration", "build_explanation", "narrate_graph_path"]


@dataclass(frozen=True, slots=True)
class GraphNarration:
    """A graph contribution, rendered as a causal chain."""

    chain: tuple[str, ...]
    confidence: float
    evidence_id: str
    evidence_level: str | None = None

    def render(self) -> str:
        """``A → B → C (confidence 0.88, label warning)``."""
        arrow = " → ".join(self.chain)
        attribution = f"confidence {self.confidence:.2f}"
        if self.evidence_level:
            attribution += f", {self.evidence_level}"
        return f"{arrow} ({attribution})"


@dataclass(frozen=True, slots=True)
class Explanation:
    """Everything needed to review an answer."""

    evidence: tuple[Evidence, ...] = ()
    graph_paths: tuple[GraphNarration, ...] = ()
    reasoning_steps: tuple[str, ...] = ()
    confidence: ConfidenceBreakdown = field(default_factory=ConfidenceBreakdown)
    uncertainty: str = ""
    dropped_claims: tuple[str, ...] = ()
    """Claims that failed verification. Reported rather than hidden: what the system declined
    to say is part of understanding what it did say."""

    def citation_map(self) -> dict[int, Evidence]:
        """1-based citation index to evidence, in presentation order."""
        return dict(enumerate(self.evidence, start=1))

    def render_markdown(self) -> str:
        """The explanation as a clinician-facing block."""
        lines: list[str] = []

        if self.evidence:
            lines.append("**Evidence**")
            for index, item in enumerate(self.evidence, start=1):
                lines.append(f"{index}. [{item.cite_label()}] {_clip(item.content, 220)}")
            lines.append("")

        if self.graph_paths:
            lines.append("**Knowledge graph**")
            for narration in self.graph_paths:
                lines.append(f"- {narration.render()}")
            lines.append("")

        if self.reasoning_steps:
            lines.append("**How this was produced**")
            lines.extend(f"{i}. {step}" for i, step in enumerate(self.reasoning_steps, start=1))
            lines.append("")

        lines.append(f"**Confidence** {self.confidence.score:.2f}")
        for name, value in sorted(self.confidence.as_dict().items()):
            lines.append(f"- {name.replace('_', ' ')}: {value:.2f}")

        if self.uncertainty:
            lines.append("")
            lines.append(f"**Uncertainty** {self.uncertainty}")

        if self.dropped_claims:
            lines.append("")
            lines.append("**Not asserted** (failed verification against the cited evidence)")
            lines.extend(f"- {note}" for note in self.dropped_claims)

        return "\n".join(lines)


def narrate_graph_path(evidence: Evidence) -> GraphNarration | None:
    """Turn a graph evidence item into a readable chain.

    The stored form is ``a treats b then b causes c``. Splitting on ``then`` and taking the
    subject of each hop recovers the chain, which is what makes a multi-hop inference
    reviewable — a clinician can reject one link rather than the whole conclusion.
    """
    if evidence.kind is not EvidenceKind.GRAPH_RELATIONSHIP:
        return None

    hops = [hop.strip() for hop in evidence.content.split(" then ") if hop.strip()]
    if not hops:
        return None

    chain: list[str] = []
    for hop in hops:
        parts = hop.split()
        if len(parts) < 3:
            chain.append(hop)
            continue
        subject, obj = parts[0], parts[-1]
        predicate = " ".join(parts[1:-1])
        if not chain:
            chain.append(_pretty(subject))
        chain.append(f"[{predicate}] {_pretty(obj)}")

    return GraphNarration(
        chain=tuple(chain),
        confidence=evidence.confidence,
        evidence_id=evidence.id,
        evidence_level=evidence.provenance.get("evidence_level"),
    )


def build_explanation(
    *,
    evidence: tuple[Evidence, ...],
    claims: tuple[Claim, ...],
    confidence: ConfidenceBreakdown,
    trace: tuple[StageRecord, ...],
    dropped: tuple[str, ...] = (),
    uncertainty: str = "",
) -> Explanation:
    """Assemble the explanation for one answer.

    Only *cited* evidence appears. The evidence set is what retrieval and the tools produced;
    presenting all of it as though the answer rested on all of it would overstate support in
    the same way a padded bibliography does.
    """
    cited_ids = {eid for claim in claims for eid in claim.evidence_ids}
    cited = tuple(item for item in evidence if item.id in cited_ids)

    narrations = tuple(
        narration
        for narration in (narrate_graph_path(item) for item in cited)
        if narration is not None
    )

    steps = tuple(f"{record.stage}: {record.summary}" for record in trace if record.summary)

    return Explanation(
        evidence=cited,
        graph_paths=narrations,
        reasoning_steps=steps,
        confidence=confidence,
        uncertainty=uncertainty or _describe_uncertainty(confidence),
        dropped_claims=dropped,
    )


def _describe_uncertainty(confidence: ConfidenceBreakdown) -> str:
    """Name the weakest component in words a clinician can act on.

    "Confidence 0.41" tells a reader nothing to do. "Coverage is the weakest part — the
    evidence addresses only part of the question" tells them to narrow it.
    """
    if confidence.score >= 0.75:
        return ""

    weakest = confidence.weakest()
    reasons = {
        "evidence_strength": ("the supporting evidence is indirect rather than a direct statement"),
        "agreement": "only one kind of source was available, so nothing corroborates it",
        "coverage": "the evidence addresses only part of what was asked",
        "recency": "the evidence is old relative to the question",
        "source_quality": "the sources are lower-grade than a guideline or a clinician's note",
        "verification": "some claims did not survive checking against their cited evidence",
    }
    return f"{weakest.replace('_', ' ')} is the weakest part — {reasons[weakest]}"


def _pretty(key: str) -> str:
    """``rx:lisinopril`` → ``lisinopril``. Ontology prefixes are noise to a reader."""
    return key.split(":")[-1].replace("_", " ")


def _clip(text: str, limit: int) -> str:
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"
