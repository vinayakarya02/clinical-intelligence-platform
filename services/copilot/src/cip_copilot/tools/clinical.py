"""The clinical tools.

Ten capabilities the planner can compose. Each is small, each returns :class:`Evidence` with
provenance rather than prose, and each declares what it touches so the registry can authorise
it (:mod:`cip_copilot.tools.base`).

Two conventions run through all of them.

**A tool reports absence explicitly.** Returning an empty result where a value was expected
is a finding — the downstream safety layer turns "no potassium recorded" into an
insufficient-evidence response rather than letting the answer quietly omit it.

**A tool never interprets.** ``lab_trend`` reports direction and magnitude; it does not say
whether the trend is concerning. Interpretation requires the full clinical picture, and a tool
that offers it invites an answer that treats one axis as a conclusion.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from cip_copilot.domain import Evidence, EvidenceKind
from cip_copilot.records import ClinicalDataSource, ObservationRecord
from cip_copilot.tools.base import (
    PhiClass,
    ToolContext,
    ToolError,
    ToolResult,
    ToolSpec,
)

__all__ = [
    "DiagnosisLookupTool",
    "DocumentSearchTool",
    "DrugInteractionTool",
    "GraphTraversalTool",
    "GuidelineLookupTool",
    "LabTrendTool",
    "MedicationLookupTool",
    "PatientLookupTool",
    "RiskScoreTool",
    "TimelineTool",
    "build_clinical_toolset",
]

_PATIENT_ID_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"patient_id": {"type": "string"}},
    "required": ["patient_id"],
}


def _patient_uuid(arguments: dict[str, Any], tool_name: str) -> uuid.UUID:
    """Parse the patient id, refusing anything malformed.

    A tool that silently accepts a bad id would query for a patient that cannot exist and
    report "no records found" — indistinguishable, to the clinician, from a patient who
    genuinely has none.
    """
    raw = str(arguments["patient_id"])
    try:
        return uuid.UUID(raw)
    except ValueError as exc:
        raise ToolError(f"'{raw}' is not a valid patient id", tool_name=tool_name) from exc


def _evidence(
    *,
    identifier: str,
    content: str,
    tenant_id: uuid.UUID,
    kind: EvidenceKind = EvidenceKind.STRUCTURED_FACT,
    source_ref: str,
    patient_id: uuid.UUID | None = None,
    effective: dt.date | None = None,
    confidence: float = 1.0,
    provenance: dict[str, Any] | None = None,
) -> Evidence:
    return Evidence(
        id=identifier,
        kind=kind,
        content=content,
        tenant_id=tenant_id,
        source_ref=source_ref,
        patient_id=patient_id,
        effective_date=effective,
        confidence=confidence,
        provenance=provenance or {},
    )


class PatientLookupTool:
    """Demographics for one patient."""

    def __init__(self, source: ClinicalDataSource) -> None:
        self._source = source

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="patient_lookup",
            description="Look up a patient's demographics (name, age, sex, MRN).",
            parameters=_PATIENT_ID_SCHEMA,
            phi_class=PhiClass.PATIENT,
        )

    async def run(self, arguments: dict[str, Any], *, context: ToolContext) -> ToolResult:
        patient_id = _patient_uuid(arguments, self.spec.name)
        patient = await self._source.get_patient(patient_id, tenant_id=context.tenant_id)
        if patient is None:
            return ToolResult(tool_name=self.spec.name, note="No such patient in this tenant.")

        age = patient.age_on(dt.date.today())
        described = f"{patient.display_name}"
        if age is not None:
            described += f", age {age}"
        if patient.sex:
            described += f", {patient.sex}"

        return ToolResult(
            tool_name=self.spec.name,
            evidence=(
                _evidence(
                    identifier=f"patient:{patient_id}",
                    content=described,
                    tenant_id=context.tenant_id,
                    source_ref=f"Patient/{patient_id}",
                    patient_id=patient_id,
                ),
            ),
            data={"age": age, "sex": patient.sex, "display_name": patient.display_name},
        )


class DiagnosisLookupTool:
    """A patient's conditions."""

    def __init__(self, source: ClinicalDataSource) -> None:
        self._source = source

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="diagnosis_lookup",
            description="List a patient's diagnoses, optionally only active ones.",
            parameters={
                "type": "object",
                "properties": {
                    "patient_id": {"type": "string"},
                    "active_only": {"type": "boolean"},
                },
                "required": ["patient_id"],
            },
            phi_class=PhiClass.PATIENT,
        )

    async def run(self, arguments: dict[str, Any], *, context: ToolContext) -> ToolResult:
        patient_id = _patient_uuid(arguments, self.spec.name)
        active_only = bool(arguments.get("active_only", False))
        conditions = await self._source.get_conditions(patient_id, tenant_id=context.tenant_id)
        if active_only:
            conditions = [c for c in conditions if c.is_active]

        if not conditions:
            return ToolResult(tool_name=self.spec.name, note="No diagnoses recorded.")

        evidence = tuple(
            _evidence(
                identifier=f"condition:{c.condition_id}",
                content=(
                    f"{c.display}"
                    + (f" ({c.code_system} {c.code})" if c.code else "")
                    + (f", onset {c.onset.isoformat()}" if c.onset else "")
                    + f", status {c.clinical_status}"
                ),
                tenant_id=context.tenant_id,
                source_ref=f"Condition/{c.condition_id}",
                patient_id=patient_id,
                effective=c.onset,
            )
            for c in conditions
        )
        return ToolResult(
            tool_name=self.spec.name,
            evidence=evidence,
            data={"conditions": [c.display for c in conditions]},
        )


class MedicationLookupTool:
    """A patient's medications."""

    def __init__(self, source: ClinicalDataSource) -> None:
        self._source = source

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="medication_lookup",
            description="List a patient's medications with dose and frequency.",
            parameters={
                "type": "object",
                "properties": {
                    "patient_id": {"type": "string"},
                    "active_only": {"type": "boolean"},
                },
                "required": ["patient_id"],
            },
            phi_class=PhiClass.PATIENT,
        )

    async def run(self, arguments: dict[str, Any], *, context: ToolContext) -> ToolResult:
        patient_id = _patient_uuid(arguments, self.spec.name)
        active_only = bool(arguments.get("active_only", False))
        medications = await self._source.get_medications(patient_id, tenant_id=context.tenant_id)
        if active_only:
            medications = [m for m in medications if m.is_active]

        if not medications:
            return ToolResult(tool_name=self.spec.name, note="No medications recorded.")

        evidence = tuple(
            _evidence(
                identifier=f"medication:{m.medication_id}",
                content=(
                    f"{m.display}"
                    + (f" {m.dose}" if m.dose else "")
                    + (f" {m.frequency}" if m.frequency else "")
                    + f", status {m.status}"
                ),
                tenant_id=context.tenant_id,
                source_ref=f"MedicationStatement/{m.medication_id}",
                patient_id=patient_id,
                effective=m.start_date,
            )
            for m in medications
        )
        return ToolResult(
            tool_name=self.spec.name,
            evidence=evidence,
            data={
                "medications": [m.display for m in medications],
                "rxnorm_codes": [m.rxnorm_code for m in medications if m.rxnorm_code],
            },
        )


class LabTrendTool:
    """Direction and magnitude of a lab series."""

    def __init__(self, source: ClinicalDataSource) -> None:
        self._source = source

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="lab_trend",
            description="Analyse the trend in a named lab value for a patient.",
            parameters={
                "type": "object",
                "properties": {
                    "patient_id": {"type": "string"},
                    "analyte": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 2, "maximum": 50},
                },
                "required": ["patient_id", "analyte"],
            },
            phi_class=PhiClass.PATIENT,
        )

    async def run(self, arguments: dict[str, Any], *, context: ToolContext) -> ToolResult:
        patient_id = _patient_uuid(arguments, self.spec.name)
        analyte = str(arguments["analyte"])
        limit = int(arguments.get("limit", 10))

        series = await self._source.get_observations(
            patient_id, tenant_id=context.tenant_id, display=analyte
        )
        series = series[-limit:]

        if not series:
            return ToolResult(tool_name=self.spec.name, note=f"No {analyte} results recorded.")
        if len(series) == 1:
            # One point is a value, not a trend. Saying "stable" from a single reading would
            # be an invention, so the single value is reported and the trend is withheld.
            only = series[0]
            return ToolResult(
                tool_name=self.spec.name,
                evidence=(_observation_evidence(only, context.tenant_id),),
                data={"direction": "unknown", "points": 1, "latest": only.value},
                note="Only one result; a trend needs at least two.",
            )

        first, last = series[0], series[-1]
        delta = last.value - first.value
        # 5% of the starting value, so the threshold scales with the analyte: 0.3 is noise
        # for a glucose and a substantial move for a potassium.
        threshold = abs(first.value) * 0.05
        direction = "stable" if abs(delta) <= threshold else ("rising" if delta > 0 else "falling")

        summary = (
            f"{last.display} {direction}: {first.value} {first.unit} on "
            f"{first.effective.isoformat()} to {last.value} {last.unit} on "
            f"{last.effective.isoformat()} ({len(series)} results)"
        )
        evidence = (
            _evidence(
                identifier=f"labtrend:{patient_id}:{analyte.lower()}",
                content=summary,
                tenant_id=context.tenant_id,
                kind=EvidenceKind.TOOL_RESULT,
                source_ref=f"lab_trend/{analyte}",
                patient_id=patient_id,
                effective=last.effective,
            ),
            *(_observation_evidence(o, context.tenant_id) for o in series),
        )
        return ToolResult(
            tool_name=self.spec.name,
            evidence=evidence,
            data={
                "direction": direction,
                "points": len(series),
                "first": first.value,
                "latest": last.value,
                "delta": round(delta, 4),
                "unit": last.unit,
                "flags": [o.flag for o in series],
            },
        )


def _observation_evidence(observation: ObservationRecord, tenant_id: uuid.UUID) -> Evidence:
    return _evidence(
        identifier=f"observation:{observation.observation_id}",
        content=observation.describe(),
        tenant_id=tenant_id,
        source_ref=f"Observation/{observation.observation_id}",
        patient_id=observation.patient_id,
        effective=observation.effective,
    )


class TimelineTool:
    """Chronological reconstruction across record types."""

    def __init__(self, source: ClinicalDataSource) -> None:
        self._source = source

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="timeline_reconstruct",
            description="Build a chronological history from encounters, conditions, "
            "medications, and observations.",
            parameters={
                "type": "object",
                "properties": {
                    "patient_id": {"type": "string"},
                    "tracks": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": ["encounter", "condition", "medication", "observation"],
                        },
                    },
                },
                "required": ["patient_id"],
            },
            phi_class=PhiClass.PATIENT,
            cost_hint_ms=15.0,
        )

    async def run(self, arguments: dict[str, Any], *, context: ToolContext) -> ToolResult:
        from cip_copilot.timeline.builder import build_timeline

        patient_id = _patient_uuid(arguments, self.spec.name)
        tracks = tuple(arguments.get("tracks") or ())

        timeline = await build_timeline(
            self._source, patient_id=patient_id, tenant_id=context.tenant_id, tracks=tracks
        )
        if not timeline.events:
            return ToolResult(tool_name=self.spec.name, note="No dated records for this patient.")

        evidence = tuple(
            _evidence(
                identifier=f"timeline:{event.event_id}",
                content=event.describe(),
                tenant_id=context.tenant_id,
                kind=EvidenceKind.TOOL_RESULT,
                source_ref=event.source_ref,
                patient_id=patient_id,
                effective=event.occurred,
            )
            for event in timeline.events
        )
        return ToolResult(
            tool_name=self.spec.name,
            evidence=evidence,
            data={"events": len(timeline.events), "tracks": sorted(timeline.tracks())},
        )


class DrugInteractionTool:
    """Interaction check against the knowledge graph."""

    def __init__(self, graph_store: Any, source: ClinicalDataSource | None = None) -> None:
        self._graph = graph_store
        self._source = source

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="drug_interaction_check",
            description="Check a set of medications for known interactions and contraindications.",
            parameters={
                "type": "object",
                "properties": {
                    "medications": {"type": "array", "items": {"type": "string"}},
                    "text": {"type": "string"},
                    "patient_id": {"type": "string"},
                },
                # Neither list nor text is required on its own: the caller may name the drugs
                # explicitly, or hand over the question and let the graph resolve them. One of
                # the two must be usable, which `run` enforces.
                "required": [],
            },
            phi_class=PhiClass.REFERENCE,
            cost_hint_ms=25.0,
        )

    async def _named_in(self, text: str, *, tenant_id: Any) -> list[str]:
        """Drug concepts the graph recognises in ``text``.

        Entity linking belongs here rather than in the planner: the graph is what knows which
        strings are drugs, and a regex in the planner would be a second, worse drug
        vocabulary that drifts from it.
        """
        from cip_retrieval.graph.schema import NodeLabel

        found: list[str] = []
        for token in {t.strip(" ,.?;:").lower() for t in text.split() if len(t.strip()) > 3}:
            nodes = await self._graph.find_nodes(
                tenant_id=tenant_id, label=NodeLabel.RXNORM_CONCEPT, text=token, limit=1
            )
            found.extend(node.key.split(":")[-1].lower() for node in nodes)
        return sorted(set(found))

    async def run(self, arguments: dict[str, Any], *, context: ToolContext) -> ToolResult:
        from cip_retrieval.graph.schema import NodeLabel, RelationshipType

        medications = [
            str(m).strip().lower() for m in (arguments.get("medications") or []) if str(m).strip()
        ]

        # Resolve drugs named directly in the question against the graph. Without this the
        # tool could only check medications some *other* step had already looked up, so
        # "do lisinopril and spironolactone interact?" — the plainest form of the most
        # safety-critical question this system answers — returned no evidence at all.
        text = str(arguments.get("text") or "").strip()
        if text:
            named = await self._named_in(text, tenant_id=context.tenant_id)
            medications = sorted({*medications, *named})

        if len(medications) < 2:
            return ToolResult(
                tool_name=self.spec.name,
                note=(
                    "An interaction check needs at least two identifiable medications; "
                    f"{len(medications)} were found."
                ),
            )

        interacting = (
            RelationshipType.INTERACTS_WITH,
            RelationshipType.CONTRAINDICATED_WITH,
        )
        wanted = set(medications)
        evidence: list[Evidence] = []
        pairs: list[dict[str, Any]] = []

        for name in medications:
            nodes = await self._graph.find_nodes(
                tenant_id=context.tenant_id,
                label=NodeLabel.RXNORM_CONCEPT,
                text=name,
                limit=2,
            )
            for node in nodes:
                edges = await self._graph.neighbours(
                    label=node.label,
                    key=node.key,
                    tenant_id=context.tenant_id,
                    relationship_types=interacting,
                )
                for edge in edges:
                    partner = edge.neighbour_key.lower()
                    # Only report pairs where *both* drugs were asked about. A drug that
                    # interacts with something the patient is not taking is noise that
                    # crowds out the pair that matters.
                    if not any(m in partner for m in wanted if m != name):
                        continue
                    statement = (
                        f"{node.key} {edge.relationship_type.value.replace('_', ' ').lower()} "
                        f"{edge.neighbour_key}"
                    )
                    evidence.append(
                        _evidence(
                            identifier=f"interaction:{node.key}:{edge.neighbour_key}",
                            content=statement,
                            tenant_id=context.tenant_id,
                            kind=EvidenceKind.GRAPH_RELATIONSHIP,
                            source_ref=f"graph/{node.key}/{edge.relationship_type.value}",
                            confidence=edge.confidence,
                            provenance={
                                "evidence_level": edge.evidence_level,
                                "source_document_id": (
                                    str(edge.source_document_id)
                                    if edge.source_document_id
                                    else None
                                ),
                            },
                        )
                    )
                    pairs.append(
                        {
                            "left": node.key,
                            "right": edge.neighbour_key,
                            "relationship": edge.relationship_type.value,
                            "confidence": edge.confidence,
                        }
                    )

        if not evidence:
            # "No interaction found" is not "no interaction exists". The note says so,
            # because a clinician reading a bare negative may act on it.
            return ToolResult(
                tool_name=self.spec.name,
                data={"pairs": [], "checked": medications},
                note="No interaction recorded in the knowledge graph for these medications. "
                "Absence of a recorded interaction is not evidence of safety.",
            )

        deduped = {item.id: item for item in evidence}
        return ToolResult(
            tool_name=self.spec.name,
            evidence=tuple(deduped.values()),
            data={"pairs": pairs, "checked": medications},
        )


class GraphTraversalTool:
    """Multi-hop traversal from a named clinical concept."""

    def __init__(self, graph_store: Any) -> None:
        self._graph = graph_store

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="graph_traversal",
            description="Traverse the clinical knowledge graph from a named concept.",
            parameters={
                "type": "object",
                "properties": {
                    "concept": {"type": "string"},
                    "max_hops": {"type": "integer", "minimum": 1, "maximum": 3},
                },
                "required": ["concept"],
            },
            phi_class=PhiClass.REFERENCE,
            cost_hint_ms=30.0,
        )

    async def run(self, arguments: dict[str, Any], *, context: ToolContext) -> ToolResult:
        from cip_retrieval.graph.traversal import TraversalOptions, traverse

        concept = str(arguments["concept"])
        max_hops = int(arguments.get("max_hops", 2))

        nodes = await self._graph.find_nodes(tenant_id=context.tenant_id, text=concept, limit=2)
        if not nodes:
            return ToolResult(
                tool_name=self.spec.name, note=f"No graph concept matches '{concept}'."
            )

        evidence: list[Evidence] = []
        for node in nodes:
            paths = await traverse(
                self._graph,
                label=node.label,
                key=node.key,
                tenant_id=context.tenant_id,
                options=TraversalOptions(max_hops=max_hops),
            )
            for path in paths:
                evidence.append(
                    _evidence(
                        identifier=f"path:{node.key}:{path.end_key}",
                        content=" then ".join(path.as_sentences()),
                        tenant_id=context.tenant_id,
                        kind=EvidenceKind.GRAPH_RELATIONSHIP,
                        source_ref=f"graph/{node.key}->{path.end_key}",
                        confidence=path.confidence,
                        provenance={"hops": path.hops, "end_key": path.end_key},
                    )
                )

        deduped = {item.id: item for item in evidence}
        return ToolResult(
            tool_name=self.spec.name,
            evidence=tuple(deduped.values()),
            data={"entry_points": [n.key for n in nodes], "paths": len(deduped)},
        )


class DocumentSearchTool:
    """Hybrid retrieval over the document corpus."""

    def __init__(self, retrieval_pipeline: Any, tenant_context_factory: Any) -> None:
        self._pipeline = retrieval_pipeline
        self._context_factory = tenant_context_factory

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="document_search",
            description="Search clinical documents for passages relevant to a query.",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "top_k": {"type": "integer", "minimum": 1, "maximum": 25},
                },
                "required": ["query"],
            },
            phi_class=PhiClass.PATIENT,
            cost_hint_ms=40.0,
        )

    async def run(self, arguments: dict[str, Any], *, context: ToolContext) -> ToolResult:
        from cip_retrieval.domain import RetrievalQuery

        query = str(arguments["query"])
        top_k = int(arguments.get("top_k", 8))

        response = await self._pipeline.retrieve(
            RetrievalQuery(
                text=query,
                tenant_id=context.tenant_id,
                patient_id=context.patient_id,
                top_k=top_k,
            ),
            context=self._context_factory(context.tenant_id),
        )

        evidence = tuple(
            Evidence(
                id=f"chunk:{candidate.id}",
                kind=EvidenceKind.DOCUMENT_CHUNK,
                content=candidate.text,
                tenant_id=context.tenant_id,
                source_ref=candidate.id,
                document_id=candidate.document_id,
                section=candidate.section_name,
                document_type=candidate.document_type,
                effective_date=candidate.effective_date,
                retrieval_score=candidate.rerank_score or candidate.fused_score,
                provenance={"strategies": sorted(candidate.ranks)},
            )
            for candidate in response.candidates
            if candidate.text.strip()
        )
        return ToolResult(
            tool_name=self.spec.name,
            evidence=evidence,
            data={
                "intent": str(response.trace.intent),
                "degraded": list(response.degraded_strategies),
            },
        )


class GuidelineLookupTool:
    """Clinical guidance for a condition.

    Backed by an injected corpus rather than a bundled one: guidelines are versioned,
    jurisdiction-specific, and often licensed, so hardcoding them would ship a claim about
    clinical practice that nobody reviewed.
    """

    def __init__(self, guidelines: dict[str, str] | None = None) -> None:
        self._guidelines = {k.lower(): v for k, v in (guidelines or {}).items()}

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="guideline_lookup",
            description="Retrieve clinical guideline text for a condition or topic.",
            parameters={
                "type": "object",
                "properties": {"topic": {"type": "string"}},
                "required": ["topic"],
            },
            phi_class=PhiClass.REFERENCE,
        )

    async def run(self, arguments: dict[str, Any], *, context: ToolContext) -> ToolResult:
        topic = str(arguments["topic"]).strip().lower()
        matches = {k: v for k, v in self._guidelines.items() if topic in k or k in topic}
        if not matches:
            return ToolResult(tool_name=self.spec.name, note=f"No guideline loaded for '{topic}'.")

        evidence = tuple(
            _evidence(
                identifier=f"guideline:{name}",
                content=text,
                tenant_id=context.tenant_id,
                kind=EvidenceKind.DOCUMENT_CHUNK,
                source_ref=f"guideline/{name}",
                provenance={"document_type": "guideline"},
            )
            for name, text in sorted(matches.items())
        )
        return ToolResult(
            tool_name=self.spec.name, evidence=evidence, data={"topics": sorted(matches)}
        )


class RiskScoreTool:
    """Published risk scores computed from structured data.

    Only scores whose formula is public and deterministic. A model-derived risk estimate
    would be a regulated claim about a patient, not a calculation, and it does not belong
    behind a tool call.
    """

    def __init__(self, source: ClinicalDataSource) -> None:
        self._source = source

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="risk_score",
            description="Compute a published clinical risk score for a patient.",
            parameters={
                "type": "object",
                "properties": {
                    "patient_id": {"type": "string"},
                    "score": {"type": "string", "enum": ["chads2_vasc"]},
                },
                "required": ["patient_id", "score"],
            },
            phi_class=PhiClass.PATIENT,
        )

    async def run(self, arguments: dict[str, Any], *, context: ToolContext) -> ToolResult:
        patient_id = _patient_uuid(arguments, self.spec.name)
        score_name = str(arguments["score"])

        patient = await self._source.get_patient(patient_id, tenant_id=context.tenant_id)
        if patient is None:
            return ToolResult(tool_name=self.spec.name, note="No such patient in this tenant.")

        conditions = await self._source.get_conditions(patient_id, tenant_id=context.tenant_id)
        active = [c.display.lower() for c in conditions if c.is_active]
        age = patient.age_on(dt.date.today())

        if age is None:
            # A missing birth date makes the score wrong, not approximate. Returning a
            # partial score would be reported as a score.
            return ToolResult(
                tool_name=self.spec.name,
                note=f"Cannot compute {score_name}: the patient's birth date is not recorded.",
            )

        components: dict[str, int] = {
            "congestive_heart_failure": int(any("heart failure" in c for c in active)),
            "hypertension": int(any("hypertension" in c for c in active)),
            "age_75_plus": 2 if age >= 75 else 0,
            "diabetes": int(any("diabetes" in c for c in active)),
            "stroke_or_tia": 2 if any("stroke" in c or "tia" in c for c in active) else 0,
            "vascular_disease": int(any("vascular" in c for c in active)),
            "age_65_74": 1 if 65 <= age < 75 else 0,
            "sex_female": int((patient.sex or "").lower().startswith("f")),
        }
        total = sum(components.values())
        contributing = sorted(name for name, value in components.items() if value)

        summary = f"CHA2DS2-VASc = {total} (from {', '.join(contributing) or 'no risk factors'})"
        return ToolResult(
            tool_name=self.spec.name,
            evidence=(
                _evidence(
                    identifier=f"riskscore:{score_name}:{patient_id}",
                    content=summary,
                    tenant_id=context.tenant_id,
                    kind=EvidenceKind.TOOL_RESULT,
                    source_ref=f"risk_score/{score_name}",
                    patient_id=patient_id,
                ),
            ),
            data={"score": total, "components": components, "contributing": contributing},
        )


def build_clinical_toolset(
    *,
    source: ClinicalDataSource,
    graph_store: Any | None = None,
    retrieval_pipeline: Any | None = None,
    tenant_context_factory: Any | None = None,
    guidelines: dict[str, str] | None = None,
) -> list[Any]:
    """Assemble the tools whose dependencies are available.

    A tool whose backing service is absent is *not registered*, rather than registered and
    failing when called. The planner only ever sees capabilities that actually work, so a plan
    cannot be built around a tool that will certainly fail — which is otherwise a confusing
    class of runtime error.
    """
    tools: list[Any] = [
        PatientLookupTool(source),
        DiagnosisLookupTool(source),
        MedicationLookupTool(source),
        LabTrendTool(source),
        TimelineTool(source),
        RiskScoreTool(source),
        GuidelineLookupTool(guidelines),
    ]
    if graph_store is not None:
        tools.append(DrugInteractionTool(graph_store, source))
        tools.append(GraphTraversalTool(graph_store))
    if retrieval_pipeline is not None and tenant_context_factory is not None:
        tools.append(DocumentSearchTool(retrieval_pipeline, tenant_context_factory))
    return tools
