"""End-to-end demonstration, workflow simulation, and benchmarks.

Runs the full decision pipeline against realistic patients, applies a care pathway, exercises
the approval lifecycle, simulates event-driven workflows, and benchmarks each subsystem.

``python -m cip_decision.demo`` reproduces the Phase 5 verification run.

**The knowledge corpus this exercises has not been clinically reviewed.** The output looks
authoritative and is not; see docs/safety/clinical-safety-case.md.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import pathlib
import sys
import time
import tracemalloc
import uuid
from dataclasses import dataclass
from typing import Any

from cip_decision.approval.workflow import ApprovalWorkflow
from cip_decision.domain import (
    ClinicalFact,
    FactKind,
    PatientContext,
    Recommendation,
    Severity,
)
from cip_decision.drugs.intelligence import DrugIntelligence
from cip_decision.engine import DecisionEngine
from cip_decision.evaluation.harness import DecisionEvalCase, DecisionEvaluator
from cip_decision.evidence_graph.graph import EvidenceGraph
from cip_decision.factory import build_pathways, build_risk_models, build_rule_engine
from cip_decision.hooks.cards import HookType, build_card, discovery_document, service_definition
from cip_decision.knowledge.loader import load_knowledge_base
from cip_decision.pathways.engine import PathwayEngine
from cip_decision.risk.scoring import RiskScorer
from cip_decision.suppression import ClinicalRole, SuppressionPolicy, Suppressor
from cip_decision.workflow.clinical import (
    ClinicalEvent,
    ClinicalEventType,
    ClinicalWorkflow,
)

TENANT = uuid.UUID("11111111-1111-4111-8111-111111111111")
PATIENT = uuid.UUID("33333333-3333-4333-8333-333333333333")
TODAY = dt.date(2026, 3, 20)

CORPUS = pathlib.Path(__file__).parent / "knowledge" / "corpus"

#: Drug classes, so class-level interaction entries match brand-name records. Configuration,
#: not code — the same reasoning as every other clinical artifact.
DRUG_CLASSES = {
    "lisinopril": "ace-inhibitor",
    "ramipril": "ace-inhibitor",
    "enalapril": "ace-inhibitor",
    "losartan": "arb",
    "valsartan": "arb",
    "spironolactone": "aldosterone-antagonist",
    "eplerenone": "aldosterone-antagonist",
    "ibuprofen": "nsaid",
    "naproxen": "nsaid",
    "diclofenac": "nsaid",
    "furosemide": "loop-diuretic",
    "indapamide": "thiazide-like-diuretic",
    "metformin": "biguanide",
    "warfarin": "vitamin-k-antagonist",
    "simvastatin": "statin",
    "atorvastatin": "statin",
    "clarithromycin": "macrolide",
}

ORGAN_ADJUSTMENTS = (
    {
        "id": "metformin-renal",
        "drug": "metformin",
        "organ": "renal",
        "marker": "egfr",
        "below": 45,
        "severity": "moderate",
        "evidence_quality": "established",
        "management": (
            "Review the metformin dose against renal function. This engine flags the need for "
            "review; it does not calculate an adjusted dose."
        ),
        "citations": [
            {"source": "FDA prescribing information", "reference": "Metformin, renal impairment"}
        ],
    },
)

DOSE_LIMITS = (
    {
        "id": "simvastatin-max",
        "drug": "simvastatin",
        "max_daily": 40,
        "unit": "mg",
        "severity": "moderate",
        "evidence_quality": "established",
        "management": "Review the dose; 80 mg is restricted to long-term established use.",
        "citations": [
            {"source": "FDA prescribing information", "reference": "Simvastatin, dosing"}
        ],
    },
)


@dataclass
class Bench:
    name: str
    operations: int
    seconds: float

    def render(self) -> str:
        per = (self.seconds / self.operations) * 1_000_000 if self.operations else 0.0
        rate = self.operations / self.seconds if self.seconds else 0.0
        return f"  {self.name:<44} {rate:>12,.0f} op/s {per:>10.2f} us each"


def _fact(
    kind: FactKind,
    name: str,
    *,
    value: float | None = None,
    unit: str | None = None,
    effective: dt.date | None = None,
    ref: str = "",
    attributes: dict[str, Any] | None = None,
) -> ClinicalFact:
    return ClinicalFact(
        kind=kind,
        name=name,
        value=value,
        unit=unit,
        effective=effective,
        source_ref=ref or f"{kind.value}:{name.lower().replace(' ', '-')}",
        attributes=attributes or {},
    )


def hyperkalemia_patient() -> PatientContext:
    """A patient on RAAS blockade with a rising potassium and reduced renal function."""
    return PatientContext(
        patient_id=PATIENT,
        tenant_id=TENANT,
        as_of=TODAY,
        age_years=71,
        sex="female",
        facts=(
            _fact(FactKind.CONDITION, "Hypertension", effective=dt.date(2019, 4, 12)),
            _fact(FactKind.CONDITION, "Type 2 diabetes mellitus", effective=dt.date(2020, 1, 8)),
            _fact(FactKind.CONDITION, "Chronic kidney disease", effective=dt.date(2024, 6, 1)),
            _fact(FactKind.MEDICATION, "Lisinopril 10 mg", effective=dt.date(2019, 5, 1)),
            _fact(FactKind.MEDICATION, "Spironolactone 25 mg", effective=dt.date(2025, 11, 3)),
            _fact(FactKind.MEDICATION, "Metformin 500 mg", effective=dt.date(2020, 2, 1)),
            _fact(FactKind.MEDICATION, "Ibuprofen 400 mg", effective=dt.date(2026, 3, 1)),
            _fact(FactKind.MEDICATION, "Furosemide 40 mg", effective=dt.date(2025, 12, 1)),
            _fact(
                FactKind.OBSERVATION,
                "Potassium",
                value=4.1,
                unit="mmol/L",
                effective=dt.date(2025, 9, 2),
                ref="obs:k1",
            ),
            _fact(
                FactKind.OBSERVATION,
                "Potassium",
                value=4.8,
                unit="mmol/L",
                effective=dt.date(2026, 1, 15),
                ref="obs:k2",
            ),
            _fact(
                FactKind.OBSERVATION,
                "Potassium",
                value=5.4,
                unit="mmol/L",
                effective=dt.date(2026, 3, 14),
                ref="obs:k3",
            ),
            _fact(
                FactKind.OBSERVATION,
                "eGFR",
                value=38,
                unit="mL/min/1.73m2",
                effective=dt.date(2026, 3, 14),
                ref="obs:egfr",
            ),
            _fact(
                FactKind.OBSERVATION,
                "Creatinine",
                value=1.4,
                unit="mg/dL",
                effective=dt.date(2026, 3, 14),
                ref="obs:cr",
            ),
        ),
    )


def thin_record_patient() -> PatientContext:
    """A patient on RAAS blockade with no recent labs — exercises missing-information."""
    return PatientContext(
        patient_id=uuid.UUID("44444444-4444-4444-8444-444444444444"),
        tenant_id=TENANT,
        as_of=TODAY,
        age_years=58,
        facts=(
            _fact(FactKind.CONDITION, "Hypertension", effective=dt.date(2022, 1, 1)),
            _fact(FactKind.MEDICATION, "Ramipril 5 mg", effective=dt.date(2022, 1, 15)),
        ),
    )


def allergy_patient() -> PatientContext:
    """A patient prescribed an agent they are recorded as allergic to."""
    return PatientContext(
        patient_id=uuid.UUID("55555555-5555-4555-8555-555555555555"),
        tenant_id=TENANT,
        as_of=TODAY,
        age_years=44,
        facts=(
            _fact(
                FactKind.ALLERGY,
                "Simvastatin",
                attributes={"reaction": "myalgia", "class": "statin"},
            ),
            _fact(
                FactKind.MEDICATION,
                "Simvastatin 80 mg",
                attributes={"daily_dose": 80, "dose_unit": "mg"},
            ),
            _fact(FactKind.MEDICATION, "Clarithromycin 500 mg"),
            _fact(FactKind.MEDICATION, "Atorvastatin 20 mg"),
        ),
    )


def labelled_cases() -> list[DecisionEvalCase]:
    """The labelled evaluation suite.

    Lives here rather than in the harness because these are clinical assertions, and clinical
    content does not belong in engine code (ADR-0019). The forbidden labels are the ones worth
    reading: each is a specific wrong answer this system produced at some point during Phase 5.
    """
    return [
        DecisionEvalCase(
            case_id="hyperkalemia-on-raas",
            context=hyperkalemia_patient(),
            expected_rule_ids=frozenset(
                {
                    "hyperkalemia-on-raas-blockade",
                    "hyperkalemia-on-potassium-sparing-diuretic",
                    "rising-potassium-trend",
                    "nsaid-with-raas-and-diuretic",
                }
            ),
            # eGFR is 38, above the labelled contraindication threshold of 30. The drug layer
            # still raises the dose-review finding at 45; the *rule* must not fire, because a
            # contraindication that triggers above its own threshold is a false alarm at the
            # one severity that is never suppressed.
            forbidden_rule_ids=frozenset({"renal-impairment-metformin"}),
            # B1: this patient has no atrial fibrillation, so a stroke score derived in AF is
            # not a number about them. Scoring it anyway produced a confident "intermediate
            # risk" band that a clinician could act on.
            forbidden_risk_models=frozenset({"cha2ds2-vasc"}),
            expect_recommendations=True,
            minimum_severity=Severity.MAJOR,
            notes="The dense case. Five rules, several interactions, one inapplicable model.",
        ),
        DecisionEvalCase(
            case_id="thin-record",
            context=thin_record_patient(),
            expected_rule_ids=frozenset({"missing-baseline-renal-function"}),
            # No potassium is recorded, so every potassium rule is unevaluable. Firing one
            # would mean unknown had been read as a value.
            forbidden_rule_ids=frozenset(
                {
                    "hyperkalemia-on-raas-blockade",
                    "hyperkalemia-on-potassium-sparing-diuretic",
                    "rising-potassium-trend",
                }
            ),
            forbidden_risk_models=frozenset({"cha2ds2-vasc"}),
            expected_missing=frozenset({"potassium"}),
            expect_recommendations=True,
            # Scored as a pharmacist. The monitoring recommendation this case is about is
            # moderate, and a prescriber's floor is major — so under the default policy the
            # case correctly produces nothing to show. Labelling it against the role that is
            # meant to see it keeps the case about the rule rather than about the floor.
            policy=SuppressionPolicy(role=ClinicalRole.PHARMACIST),
            notes="Asserts the engine says what it could not evaluate rather than staying quiet.",
        ),
        DecisionEvalCase(
            case_id="severe-renal-impairment",
            context=PatientContext(
                patient_id=uuid.UUID("77777777-7777-4777-8777-777777777777"),
                tenant_id=TENANT,
                as_of=TODAY,
                age_years=79,
                facts=(
                    _fact(FactKind.CONDITION, "Type 2 diabetes mellitus"),
                    _fact(FactKind.MEDICATION, "Metformin 1000 mg", effective=dt.date(2021, 6, 1)),
                    _fact(
                        FactKind.OBSERVATION,
                        "eGFR",
                        value=24,
                        unit="mL/min/1.73m2",
                        effective=dt.date(2026, 3, 10),
                    ),
                ),
            ),
            expected_rule_ids=frozenset({"renal-impairment-metformin"}),
            expect_recommendations=True,
            # A contraindication is exempt from every suppression mechanism. Scored as a
            # prescriber precisely because that is the most restrictive floor: if the exemption
            # ever breaks, this is the case that catches it.
            minimum_severity=Severity.CONTRAINDICATED,
            notes="The rule the coverage metric found no case exercised.",
        ),
        DecisionEvalCase(
            case_id="statin-allergy",
            context=allergy_patient(),
            # B2: an allergy to one statin is not an allergy to every statin. Cross-reactivity
            # is declared per class; statins are not declared, so only the exact ingredient
            # matches. B3 lives here too — the second statin's macrolide interaction is a
            # distinct contraindication and must not deduplicate into the first.
            forbidden_rule_ids=frozenset({"renal-impairment-metformin"}),
            expect_recommendations=True,
            minimum_severity=Severity.CONTRAINDICATED,
            notes="Exercises allergy matching, dose limits, and interaction deduplication.",
        ),
        DecisionEvalCase(
            case_id="healthy-adult",
            context=PatientContext(
                patient_id=uuid.UUID("66666666-6666-4666-8666-666666666666"),
                tenant_id=TENANT,
                as_of=TODAY,
                age_years=34,
                facts=(
                    _fact(
                        FactKind.OBSERVATION,
                        "Potassium",
                        value=4.0,
                        unit="mmol/L",
                        effective=dt.date(2026, 3, 1),
                    ),
                ),
            ),
            expect_recommendations=False,
            # Only the AF-derived model is forbidden. The AKI flag is a factor count with no
            # population precondition, so scoring zero for a patient with no risk factors is a
            # true statement rather than a meaningless one.
            forbidden_risk_models=frozenset({"cha2ds2-vasc"}),
            notes="The case an over-alerting engine fails. Nothing here warrants an alert.",
        ),
    ]


def build_engine(*, role: ClinicalRole = ClinicalRole.PRESCRIBER) -> tuple[DecisionEngine, Any]:
    base = load_knowledge_base(CORPUS)
    engine = DecisionEngine(
        rules=build_rule_engine(base),
        drugs=DrugIntelligence(
            interactions=base.interactions,
            drug_classes=DRUG_CLASSES,
            dose_limits=DOSE_LIMITS,
            organ_adjustments=ORGAN_ADJUSTMENTS,
            # Beta-lactams only. Declared, never assumed: treating every class as
            # cross-reactive denies patients needed drugs on no evidence.
            cross_reactive_classes=frozenset({"beta-lactam", "penicillin", "cephalosporin"}),
        ),
        risk=RiskScorer(build_risk_models(base)),
        pathways=PathwayEngine(build_pathways(base)),
        suppressor=Suppressor(policy=SuppressionPolicy(role=role)),
        approvals=ApprovalWorkflow(),
        graph=EvidenceGraph(),
    )
    return engine, base


def _print_decision(label: str, result: Any) -> None:
    print(f"\n{'-' * 78}\n{label}\n{'-' * 78}")
    print(
        f"rules evaluated : {len(result.rule_trace.outcomes)}  "
        f"fired: {len(result.rule_trace.fired)}  "
        f"unevaluable: {len(result.rule_trace.unevaluable)}"
    )
    print(
        f"drug findings   : {len(result.drug_report.findings)} "
        f"over {result.drug_report.pairs_checked} pair(s)"
    )
    print(f"shown           : {len(result.recommendations)}   suppressed: {len(result.suppressed)}")

    if result.recommendations:
        print("\nrecommendations (highest severity first):")
        for rec in result.recommendations:
            print(f"  [{rec.severity.value:>17}] {rec.summary}")
    if result.suppressed:
        print("\nsuppressed:")
        for rec in result.suppressed:
            print(f"  [{rec.severity.value:>17}] {rec.summary[:58]} — {rec.suppression_reason}")
    if result.summary_line:
        print(f"\n  {result.summary_line}")
    if result.contradictions.has_conflicts:
        print("\ncontradictions:")
        for conflict in result.contradictions.contradictions:
            print(f"  {conflict.render()}")
    if result.missing_information:
        print(f"\nmissing information: {', '.join(result.missing_information)}")
    for risk in result.risk_results:
        if not risk.applicable:
            # Printing a score for an inapplicable model is exactly the misleading output the
            # applicability check exists to prevent, so the display has to respect it too.
            print(f"\nrisk: {risk.model.title} — NOT SCORED ({risk.inapplicable_reason})")
            continue
        marker = "" if risk.is_complete else "  (lower bound — incomplete)"
        print(
            f"\nrisk: {risk.model.title} = {risk.score}/{risk.model.maximum} [{risk.band}]{marker}"
        )


async def run_demo() -> None:
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8", errors="replace")

    tracemalloc.start()
    benches: list[Bench] = []

    print("=" * 78)
    print("PHASE 5 END-TO-END VERIFICATION — CLINICAL DECISION INTELLIGENCE")
    print("=" * 78)
    print("\n*** The knowledge corpus exercised here has NOT been clinically reviewed. ***")
    print("*** See docs/safety/clinical-safety-case.md before reading the output.    ***")

    started = time.perf_counter()
    engine, base = build_engine()
    benches.append(Bench("engine construction", 1, time.perf_counter() - started))
    print(f"\nknowledge base: {base.describe()}")
    print(f"active rules on {TODAY}: {len(base.active_rules(TODAY))} of {len(base.rules)}")

    # ---- 1. the complex patient -----------------------------------------------------------
    patient = hyperkalemia_patient()
    started = time.perf_counter()
    result = engine.decide(patient)
    benches.append(Bench("decision: complex patient", 1, time.perf_counter() - started))
    _print_decision("PATIENT 1 — hyperkalemia on RAAS blockade, reduced eGFR, NSAID", result)

    print("\nexplanation of the highest-severity recommendation:")
    if result.recommendations:
        top = result.recommendations[0]
        print("  " + top.explain().replace("\n", "\n  "))
        print("\nevidence graph paths:")
        for path in engine.evidence_graph.explain(top.id)[:4]:
            print(f"  {path}")

    # ---- 2. role tailoring ----------------------------------------------------------------
    print(f"\n{'-' * 78}\nROLE TAILORING — the same patient, different roles\n{'-' * 78}")
    for role in (ClinicalRole.PRESCRIBER, ClinicalRole.PHARMACIST, ClinicalRole.REVIEWER):
        role_engine, _ = build_engine(role=role)
        role_result = role_engine.decide(patient)
        print(
            f"  {role.value:<12} floor={role.default_floor.value:<15} "
            f"shown={len(role_result.recommendations)} suppressed={len(role_result.suppressed)}"
        )

    # ---- 3. thin record ------------------------------------------------------------------
    started = time.perf_counter()
    thin = engine.decide(thin_record_patient())
    benches.append(Bench("decision: thin record", 1, time.perf_counter() - started))
    _print_decision("PATIENT 2 — RAAS blockade with no recent labs", thin)
    print("\n  what an empty result means:")
    print(f"  {thin.absence_statement()}")

    # ---- 4. allergy and contraindication --------------------------------------------------
    allergic = engine.decide(allergy_patient())
    _print_decision("PATIENT 3 — allergy conflict, CYP3A4 contraindication, dose limit", allergic)

    # ---- 5. care pathway -----------------------------------------------------------------
    print(f"\n{'=' * 78}\nCARE PATHWAY\n{'=' * 78}")
    started = time.perf_counter()
    applied = engine._pathways.apply("pw-hyperkalemia", patient)
    benches.append(Bench("pathway application (cold, 1 run)", 1, time.perf_counter() - started))
    print(applied.render())
    print(f"\nstages produced: {list(applied.by_stage())}")

    # ---- 6. approval lifecycle -----------------------------------------------------------
    print(f"\n{'=' * 78}\nAPPROVAL LIFECYCLE\n{'=' * 78}")
    workflow = engine.approvals
    print(f"open for review: {len(workflow.open_records())}")
    if result.recommendations:
        first = result.recommendations[0]
        workflow.claim(first.id, reviewer_id="dr-okafor")
        record = workflow.accept(first.id, reviewer_id="dr-okafor", note="agreed, repeating today")
        print(f"  accepted  {first.id[:44]} by {record.decided_by}")
    if len(result.recommendations) > 1:
        second = result.recommendations[1]
        workflow.claim(second.id, reviewer_id="dr-okafor")
        workflow.reject(second.id, reviewer_id="dr-okafor", reason="already-addressed")
        print(f"  rejected  {second.id[:44]} — already-addressed")
    print(f"  rejection reasons so far: {workflow.rejection_reasons()}")

    if len(result.recommendations) > 1:
        try:
            workflow.accept(result.recommendations[1].id, reviewer_id="dr-okafor")
        except Exception as exc:
            print(f"  re-accepting a rejected recommendation is refused: {type(exc).__name__}")

    # ---- 7. CDS Hooks --------------------------------------------------------------------
    print(f"\n{'=' * 78}\nCDS HOOKS\n{'=' * 78}")
    services = tuple(
        service_definition(
            hook=hook,
            service_id=f"cip-{hook.value}",
            title=f"Clinical Intelligence Platform — {hook.value}",
            description="Deterministic, cited clinical decision support.",
            prefetch={"patient": "Patient/{{context.patientId}}"},
        )
        for hook in HookType
    )
    document = discovery_document(services)
    print(f"discovery advertises {len(document['services'])} service(s):")
    for service in document["services"]:
        deprecated = " [DEPRECATED]" if "DEPRECATED" in service["description"] else ""
        print(f"  {service['hook']:<22} {service['id']}{deprecated}")

    if result.recommendations:
        card = build_card(result.recommendations[0])
        payload = card.to_json()
        print(f"\ncard: indicator={payload['indicator']} summary={len(payload['summary'])} chars")
        print(f"  fields: {sorted(payload)}")
        print(f"  override reasons offered: {len(payload['overrideReasons'])}")

    # ---- 8. workflow simulation ----------------------------------------------------------
    print(f"\n{'=' * 78}\nWORKFLOW SIMULATION\n{'=' * 78}")

    class _CollectingNotifier:
        def __init__(self) -> None:
            self.delivered: list[tuple[str, int]] = []

        async def notify(self, *, event: Any, recommendations: tuple[Recommendation, ...]) -> None:
            self.delivered.append((str(event.type), len(recommendations)))

    notifier = _CollectingNotifier()
    clinical = ClinicalWorkflow(engine, notifier=notifier)

    started = time.perf_counter()
    runs = []
    for event_type in (
        ClinicalEventType.LAB_RESULT_AVAILABLE,
        ClinicalEventType.MEDICATION_PRESCRIBED,
        ClinicalEventType.PERIODIC_REVIEW,
    ):
        run = await clinical.handle(
            ClinicalEvent(
                type=event_type,
                patient_id=PATIENT,
                tenant_id=TENANT,
                payload={"observation": "potassium"},
                correlation_id=f"sim-{event_type.value}",
            ),
            context=patient,
        )
        runs.append(run)
        print(
            f"  {event_type.value:<26} floor={event_type.default_urgency.value:<15} "
            f"notified={len(run.notified)} withheld={len(run.withheld)}"
        )
    benches.append(Bench("workflow event", len(runs), time.perf_counter() - started))
    print(f"\n  notifier received: {notifier.delivered}")

    # ---- 9. evaluation -------------------------------------------------------------------
    print(f"\n{'=' * 78}\nEVALUATION\n{'=' * 78}")

    eval_engine, eval_base = build_engine()
    evaluator = DecisionEvaluator(eval_engine, rules=build_rule_engine(eval_base))
    report = evaluator.run(labelled_cases())
    print(report.render())
    print(
        "\n  Recall and burden are reported side by side and never combined. The number that\n"
        "  actually matters — the real-world override rate — cannot be measured offline;\n"
        "  these burden figures are an upper bound on what would reach a screen."
    )

    # ---- 10. benchmarks ------------------------------------------------------------------
    print(f"\n{'=' * 78}\nBENCHMARKS\n{'=' * 78}")

    iterations = 300
    started = time.perf_counter()
    for _ in range(iterations):
        engine._rules.evaluate(patient)
    benches.append(Bench("rule evaluation (7 rules)", iterations, time.perf_counter() - started))

    started = time.perf_counter()
    for _ in range(iterations):
        engine._drugs.check(patient)
    benches.append(
        Bench("drug checks (5 meds, 10 pairs)", iterations, time.perf_counter() - started)
    )

    started = time.perf_counter()
    for _ in range(iterations):
        engine._risk.score_all(patient)
    benches.append(Bench("risk scoring (2 models)", iterations, time.perf_counter() - started))

    started = time.perf_counter()
    for _ in range(iterations):
        engine._pathways.apply("pw-hyperkalemia", patient)
    benches.append(Bench("pathway application", iterations, time.perf_counter() - started))

    fresh, _ = build_engine()
    started = time.perf_counter()
    for _ in range(iterations):
        fresh.decide(patient)
    benches.append(Bench("full decision pipeline", iterations, time.perf_counter() - started))

    for bench in benches:
        print(bench.render())

    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    print(f"\n  {'peak memory':<44} {peak / 1024 / 1024:>12.2f} MB")
    print(
        "\n  In-process, deterministic, no network. The decision path contains no model "
        "call\n  (ADR-0022), so these are the real latencies rather than a lower bound."
    )


def main() -> None:
    asyncio.run(run_demo())


if __name__ == "__main__":  # pragma: no cover
    main()
