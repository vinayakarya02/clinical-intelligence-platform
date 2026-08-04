"""End-to-end demonstration and benchmark for the clinical copilot.

Builds a realistic tenant — a patient with a coded record, a document corpus, and a knowledge
graph — then runs multi-turn conversations through the full pipeline and prints what happened
at every stage. Finishes with an evaluation pass and per-stage benchmarks.

``python -m cip_copilot.demo`` reproduces the Phase 3 verification run.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import sys
import time
import tracemalloc
import uuid
from dataclasses import dataclass
from typing import Any

from cip_copilot.agents.stages import StageDeps
from cip_copilot.domain import CopilotQuestion, ResponseMode
from cip_copilot.evaluation.harness import CopilotEvalCase, CopilotEvaluator, CostModel
from cip_copilot.llm.extractive import ExtractiveLanguageModel
from cip_copilot.memory.session import MemoryStore
from cip_copilot.orchestrator import ClinicalCopilot
from cip_copilot.output.renderers import render_api_envelope, render_fhir, render_markdown
from cip_copilot.planner.rule_planner import ClinicalRulePlanner
from cip_copilot.prompts.catalog import PromptCatalog
from cip_copilot.records import (
    ConditionRecord,
    EncounterRecord,
    InMemoryClinicalData,
    MedicationRecord,
    ObservationRecord,
    PatientRecord,
)
from cip_copilot.tools.base import ToolRegistry
from cip_copilot.tools.clinical import build_clinical_toolset

TENANT = uuid.UUID("11111111-1111-4111-8111-111111111111")
PATIENT = uuid.UUID("33333333-3333-4333-8333-333333333333")
SOURCE_DOC = uuid.UUID("44444444-4444-4444-8444-444444444444")

TODAY = dt.date(2026, 3, 20)


@dataclass
class Stage:
    name: str
    duration_ms: float
    detail: str = ""


def _clinical_data() -> InMemoryClinicalData:
    """A patient with hypertension, diabetes, and a rising potassium on two ACE/ARB-class drugs."""
    data = InMemoryClinicalData()
    data.add_patient(
        PatientRecord(
            patient_id=PATIENT,
            tenant_id=TENANT,
            display_name="Jordan Rivera",
            birth_date=dt.date(1963, 7, 2),
            sex="female",
            mrn="MRN-00471925",
        )
    )
    data.conditions.extend(
        [
            ConditionRecord(
                condition_id="c1",
                patient_id=PATIENT,
                display="Hypertension",
                code="38341003",
                code_system="SNOMED",
                onset=dt.date(2019, 4, 12),
                source_document_id=SOURCE_DOC,
            ),
            ConditionRecord(
                condition_id="c2",
                patient_id=PATIENT,
                display="Type 2 diabetes mellitus",
                code="44054006",
                code_system="SNOMED",
                onset=dt.date(2020, 1, 8),
            ),
            ConditionRecord(
                condition_id="c3",
                patient_id=PATIENT,
                display="Hyperkalemia",
                code="14140009",
                code_system="SNOMED",
                onset=dt.date(2026, 3, 14),
            ),
        ]
    )
    data.medications.extend(
        [
            MedicationRecord(
                medication_id="m1",
                patient_id=PATIENT,
                display="Lisinopril",
                rxnorm_code="29046",
                dose="10 mg",
                frequency="daily",
                start_date=dt.date(2019, 5, 1),
            ),
            MedicationRecord(
                medication_id="m2",
                patient_id=PATIENT,
                display="Spironolactone",
                rxnorm_code="9997",
                dose="25 mg",
                frequency="daily",
                start_date=dt.date(2025, 11, 3),
            ),
            MedicationRecord(
                medication_id="m3",
                patient_id=PATIENT,
                display="Metformin",
                rxnorm_code="6809",
                dose="500 mg",
                frequency="twice daily",
                start_date=dt.date(2020, 2, 1),
            ),
        ]
    )
    for index, (day, value) in enumerate(
        [(dt.date(2025, 9, 2), 4.1), (dt.date(2026, 1, 15), 4.8), (dt.date(2026, 3, 14), 5.4)],
        start=1,
    ):
        data.observations.append(
            ObservationRecord(
                observation_id=f"k{index}",
                patient_id=PATIENT,
                display="Potassium",
                value=value,
                unit="mmol/L",
                effective=day,
                loinc_code="2823-3",
                reference_low=3.5,
                reference_high=5.1,
                source_document_id=SOURCE_DOC,
            )
        )
    data.encounters.append(
        EncounterRecord(
            encounter_id="e1",
            patient_id=PATIENT,
            kind="inpatient admission",
            start=dt.date(2026, 3, 14),
            end=dt.date(2026, 3, 16),
            reason="chest pain",
        )
    )
    return data


async def _graph() -> Any:
    """A small drug-interaction graph."""
    from cip_retrieval.graph import (
        GraphNode,
        GraphRelationship,
        InMemoryGraphStore,
        NodeLabel,
        Provenance,
        RelationshipType,
    )

    store = InMemoryGraphStore()
    await store.upsert_nodes(
        [
            GraphNode(
                label=NodeLabel.RXNORM_CONCEPT,
                key="rx:lisinopril",
                properties={"display_text": "Lisinopril"},
            ),
            GraphNode(
                label=NodeLabel.RXNORM_CONCEPT,
                key="rx:spironolactone",
                properties={"display_text": "Spironolactone"},
            ),
            GraphNode(
                label=NodeLabel.SNOMED_CONCEPT,
                key="sct:hyperkalemia",
                properties={"display_text": "Hyperkalemia"},
            ),
        ]
    )
    provenance = Provenance(source_document_id=SOURCE_DOC, evidence_level="label_warning")
    await store.upsert_relationships(
        [
            GraphRelationship(
                type=RelationshipType.CONTRAINDICATED_WITH,
                start_label=NodeLabel.RXNORM_CONCEPT,
                start_key="rx:lisinopril",
                end_label=NodeLabel.RXNORM_CONCEPT,
                end_key="rx:spironolactone",
                confidence=0.88,
                provenance=provenance,
            ),
            GraphRelationship(
                type=RelationshipType.CAUSES,
                start_label=NodeLabel.RXNORM_CONCEPT,
                start_key="rx:spironolactone",
                end_label=NodeLabel.SNOMED_CONCEPT,
                end_key="sct:hyperkalemia",
                confidence=0.90,
                provenance=provenance,
            ),
        ]
    )
    return store


GUIDELINES = {
    "hyperkalemia": (
        "Hyperkalemia management: withhold potassium-sparing agents, recheck serum potassium "
        "within 72 hours, and review concomitant ACE inhibitor and aldosterone antagonist "
        "therapy."
    ),
    "hypertension": (
        "Hypertension: first-line therapy includes ACE inhibitors, ARBs, calcium channel "
        "blockers, and thiazide diuretics."
    ),
}


def _build_copilot(graph: Any) -> ClinicalCopilot:
    tools = build_clinical_toolset(
        source=_clinical_data(), graph_store=graph, guidelines=GUIDELINES
    )
    deps = StageDeps(
        registry=ToolRegistry(tools),
        planner=ClinicalRulePlanner(),
        memory=MemoryStore(),
        catalog=PromptCatalog(),
        model=ExtractiveLanguageModel(),
    )
    return ClinicalCopilot(deps)


def _print_turn(label: str, result: Any) -> None:
    answer = result.answer
    print(f"\n{'-' * 78}\n{label}\n{'-' * 78}")
    print(f"mode        : {answer.mode}")
    print(f"confidence  : {answer.confidence.score:.2f} (weakest: {answer.confidence.weakest()})")
    print(f"claims      : {len(answer.claims)} verified")
    print(f"evidence    : {len(answer.cited_evidence())} cited of {len(answer.evidence)} gathered")
    print(f"tokens      : {answer.usage.total_tokens} in {answer.usage.calls} model call(s)")

    stages = " → ".join(f"{r.stage}({r.duration_ms:.1f}ms)" for r in answer.trace)
    print(f"pipeline    : {stages}")

    if answer.text:
        print(f"\nanswer:\n{answer.text[:400]}")
    if answer.safety_findings:
        print("\nsafety:")
        for finding in answer.safety_findings:
            print(f"  [{finding.severity}] {finding.message}")
    if result.explanation.graph_paths:
        print("\ngraph reasoning:")
        for narration in result.explanation.graph_paths:
            print(f"  {narration.render()}")


async def run_demo() -> None:
    """Run the full copilot pipeline across several conversations."""
    tracemalloc.start()
    stages: list[Stage] = []

    print("=" * 78)
    print("PHASE 3 END-TO-END VERIFICATION — CLINICAL COPILOT")
    print("=" * 78)

    started = time.perf_counter()
    graph = await _graph()
    copilot = _build_copilot(graph)
    stages.append(Stage("copilot construction", (time.perf_counter() - started) * 1000))

    session = "demo-session-1"

    # 1. A question that needs several tools and surfaces an interaction.
    started = time.perf_counter()
    first = await copilot.ask(
        CopilotQuestion(
            text="Is the potassium trend concerning given the current medications?",
            tenant_id=TENANT,
            session_id=session,
            patient_id=PATIENT,
        )
    )
    stages.append(
        Stage("turn 1: multi-tool clinical question", (time.perf_counter() - started) * 1000)
    )
    _print_turn("TURN 1 — potassium trend on current medications", first)

    # 2. A follow-up relying on conversational memory.
    started = time.perf_counter()
    second = await copilot.ask(
        CopilotQuestion(
            text="What about her diagnoses?",
            tenant_id=TENANT,
            session_id=session,
            patient_id=PATIENT,
        )
    )
    stages.append(Stage("turn 2: memory follow-up", (time.perf_counter() - started) * 1000))
    _print_turn("TURN 2 — follow-up resolved from memory", second)

    # 3. A patient-scoped question with no patient: must ask, not guess.
    started = time.perf_counter()
    third = await copilot.ask(
        CopilotQuestion(
            text="What medications is he taking?",
            tenant_id=TENANT,
            session_id="demo-session-2",
        )
    )
    stages.append(Stage("turn 3: unresolvable reference", (time.perf_counter() - started) * 1000))
    _print_turn("TURN 3 — unresolvable reference in a fresh session", third)

    # 4. A question with no supporting evidence: must decline.
    started = time.perf_counter()
    fourth = await copilot.ask(
        CopilotQuestion(
            text="What is the patient's most recent echocardiogram ejection fraction?",
            tenant_id=TENANT,
            session_id="demo-session-3",
            patient_id=PATIENT,
        )
    )
    stages.append(Stage("turn 4: no evidence", (time.perf_counter() - started) * 1000))
    _print_turn("TURN 4 — question the record cannot answer", fourth)

    # 5. Timeline reconstruction.
    started = time.perf_counter()
    fifth = await copilot.ask(
        CopilotQuestion(
            text="Give me the clinical timeline and disease progression for this patient.",
            tenant_id=TENANT,
            session_id="demo-session-4",
            patient_id=PATIENT,
        )
    )
    stages.append(Stage("turn 5: timeline", (time.perf_counter() - started) * 1000))
    _print_turn("TURN 5 — timeline reconstruction", fifth)

    print(f"\n{'=' * 78}\nOUTPUT FORMATS\n{'=' * 78}")
    markdown = render_markdown(
        first.answer, explanation_markdown=first.explanation.render_markdown()
    )
    print(f"markdown      : {len(markdown)} chars")
    print(f"api envelope  : {sorted(render_api_envelope(first.answer))}")
    bundle = render_fhir(first.answer, patient_id=PATIENT)
    print(
        f"fhir bundle   : {bundle['resourceType']} with "
        f"{[e['resource']['resourceType'] for e in bundle['entry']]}"
    )

    print(f"\n{'=' * 78}\nEXPLANATION (turn 1)\n{'=' * 78}")
    print(first.explanation.render_markdown()[:1600])

    print(f"\n{'=' * 78}\nEVALUATION\n{'=' * 78}")
    cases = [
        CopilotEvalCase(
            case_id="potassium-on-meds",
            question="Is the potassium trend concerning given the current medications?",
            expected_mode=ResponseMode.ANSWER,
            expected_capabilities=frozenset({"lab_trend", "medication_lookup"}),
        ),
        CopilotEvalCase(
            case_id="diagnoses",
            question="What are the active diagnoses?",
            expected_mode=ResponseMode.ANSWER,
            expected_capabilities=frozenset({"diagnosis_lookup"}),
        ),
        CopilotEvalCase(
            case_id="interaction",
            question="Do any of these medications interact with each other?",
            expected_mode=ResponseMode.ANSWER,
            expected_capabilities=frozenset({"medication_lookup"}),
        ),
        CopilotEvalCase(
            case_id="timeline",
            question="Show the clinical timeline and progression.",
            expected_mode=ResponseMode.ANSWER,
            expected_capabilities=frozenset({"timeline_reconstruct"}),
        ),
        CopilotEvalCase(
            case_id="unanswerable-ejection-fraction",
            question="What is the most recent echocardiogram ejection fraction?",
            expected_mode=ResponseMode.BLOCKED,
            forbidden_substrings=("ejection fraction is", "%"),
            notes="Nothing in the record mentions an echocardiogram.",
        ),
        CopilotEvalCase(
            case_id="risk-score",
            question="What is the stroke risk score for this patient?",
            expected_mode=ResponseMode.ANSWER,
            expected_capabilities=frozenset({"risk_score"}),
        ),
    ]

    async def _ask(case: CopilotEvalCase) -> Any:
        return await copilot.ask(
            CopilotQuestion(
                text=case.question,
                tenant_id=TENANT,
                session_id=f"eval-{case.case_id}",
                patient_id=PATIENT,
            )
        )

    started = time.perf_counter()
    report = await CopilotEvaluator(cost_model=CostModel(label="extractive")).run(cases, _ask)
    stages.append(
        Stage("evaluation", (time.perf_counter() - started) * 1000, f"{len(cases)} cases")
    )
    print(report.summary())
    if report.failures():
        print("\n  failures:")
        for failure in report.failures():
            print(f"    {failure}")

    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    print(f"\n{'=' * 78}\nBENCHMARKS\n{'=' * 78}")
    for stage in stages:
        detail = f"  ({stage.detail})" if stage.detail else ""
        print(f"  {stage.name:<44} {stage.duration_ms:>8.2f} ms{detail}")
    print(f"\n  {'peak memory':<44} {peak / 1024 / 1024:>8.2f} MB")
    print(f"  {'resident at exit':<44} {current / 1024 / 1024:>8.2f} MB")
    print(
        "\n  In-process figures with a deterministic extractive model. A real provider adds\n"
        "  a network round-trip and dominates every number above."
    )


def main() -> None:
    # The explanation renders graph chains with arrows. A Windows console defaults to a
    # codepage that cannot encode them, which would crash the demo on the output rather
    # than on anything real.
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8", errors="replace")
    asyncio.run(run_demo())


if __name__ == "__main__":  # pragma: no cover
    main()
