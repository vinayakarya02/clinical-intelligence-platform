"""Clinical decision intelligence.

Most of these assert what the engine **refuses** to do — score a model outside its population,
contraindicate a whole class from one allergy, suppress a contraindication, accept a
recommendation without a human, or claim a contradiction it cannot substantiate.

Several are regressions for defects the end-to-end run exposed and unit tests did not.
"""

from __future__ import annotations

import datetime as dt
import pathlib
import uuid

import pytest

from cip_decision.approval.workflow import ApprovalError, ApprovalWorkflow
from cip_decision.contradiction import Direction, detect_contradictions
from cip_decision.domain import (
    Citation,
    ClinicalFact,
    EvidenceQuality,
    FactKind,
    PatientContext,
    ProvenanceLink,
    Recommendation,
    RecommendationKind,
    ReviewState,
    Severity,
)
from cip_decision.drugs.intelligence import DrugCheckKind, DrugIntelligence
from cip_decision.engine import DecisionEngine
from cip_decision.evidence_graph.graph import EvidenceGraph, NodeKind
from cip_decision.factory import build_pathways, build_risk_models, build_rule_engine
from cip_decision.hooks.cards import Card, HookType, Source, build_card, service_definition
from cip_decision.knowledge.loader import KnowledgeError, load_knowledge_base, parse_condition
from cip_decision.pathways.engine import Applicability, PathwayEngine
from cip_decision.risk.scoring import RiskScorer
from cip_decision.rules.ast import (
    AllOf,
    AnyOf,
    Comparison,
    HasFact,
    Not,
    Operator,
    Trend,
    TrendDirection,
    ValueOf,
)
from cip_decision.suppression import ClinicalRole, SuppressionPolicy, Suppressor

TENANT = uuid.UUID("11111111-1111-4111-8111-111111111111")
PATIENT = uuid.UUID("33333333-3333-4333-8333-333333333333")
TODAY = dt.date(2026, 3, 20)
CORPUS = pathlib.Path(__file__).resolve().parents[2] / (
    "services/decision/src/cip_decision/knowledge/corpus"
)


def _fact(kind: FactKind, name: str, **kwargs: object) -> ClinicalFact:
    return ClinicalFact(kind=kind, name=name, **kwargs)  # type: ignore[arg-type]


def _context(*facts: ClinicalFact, age: int | None = 70) -> PatientContext:
    return PatientContext(
        patient_id=PATIENT, tenant_id=TENANT, facts=facts, as_of=TODAY, age_years=age
    )


def _recommendation(
    identifier: str,
    *,
    severity: Severity = Severity.MAJOR,
    summary: str = "Do something",
    subjects: tuple[str, ...] = (),
    direction: str = "",
    concern: str = "",
    kind: RecommendationKind = RecommendationKind.ALERT,
) -> Recommendation:
    metadata: dict[str, str] = {}
    if direction:
        metadata["direction"] = direction
    if concern:
        metadata["concern"] = concern
    return Recommendation(
        id=identifier,
        kind=kind,
        summary=summary,
        severity=severity,
        evidence_quality=EvidenceQuality.ESTABLISHED,
        citations=(Citation(source="Test"),),
        provenance=(ProvenanceLink(kind="rule", identifier="r", label="test"),),
        patient_id=PATIENT,
        triggering_facts=subjects,
        metadata=metadata,
    )


class TestDomainInvariants:
    def test_a_recommendation_cannot_exist_without_a_citation(self) -> None:
        """An uncited clinical recommendation cannot be reviewed or defended."""
        with pytest.raises(ValueError, match="no citation"):
            Recommendation(
                id="r",
                kind=RecommendationKind.ALERT,
                summary="x",
                severity=Severity.MAJOR,
                evidence_quality=EvidenceQuality.ESTABLISHED,
                citations=(),
                provenance=(ProvenanceLink(kind="rule", identifier="r", label="l"),),
            )

    def test_a_recommendation_cannot_exist_without_provenance(self) -> None:
        """The structural form of "recommendations must always explain WHY"."""
        with pytest.raises(ValueError, match="no provenance"):
            Recommendation(
                id="r",
                kind=RecommendationKind.ALERT,
                summary="x",
                severity=Severity.MAJOR,
                evidence_quality=EvidenceQuality.ESTABLISHED,
                citations=(Citation(source="s"),),
                provenance=(),
            )

    def test_a_summary_too_long_for_a_card_is_refused(self) -> None:
        """Enforced at construction, so a recommendation that cannot be displayed cannot
        be created."""
        with pytest.raises(ValueError, match="140 characters"):
            _recommendation("r", summary="x" * 141)

    def test_severity_and_evidence_are_independent(self) -> None:
        """Collapsing them suppresses contraindications or shouts theoretical risks."""
        assert Severity.CONTRAINDICATED.rank > Severity.MINOR.rank
        assert EvidenceQuality.THEORETICAL.weight < EvidenceQuality.ESTABLISHED.weight
        assert EvidenceQuality.THEORETICAL.qualifier != EvidenceQuality.ESTABLISHED.qualifier

    def test_only_contraindications_are_unsuppressible(self) -> None:
        for severity in Severity:
            assert severity.is_suppressible == (severity is not Severity.CONTRAINDICATED)

    def test_major_maps_to_a_critical_card(self) -> None:
        """A card that may be life-threatening must not render like a moderate one."""
        assert Severity.MAJOR.cds_hooks_indicator == "critical"
        assert Severity.MODERATE.cds_hooks_indicator == "warning"


class TestConditionLanguage:
    def test_a_comparison_fires_above_its_threshold(self) -> None:
        context = _context(
            _fact(FactKind.OBSERVATION, "Potassium", value=5.4, unit="mmol/L", effective=TODAY)
        )
        condition = Comparison(ValueOf("potassium"), Operator.GT, 5.0, unit="mmol/L")
        assert condition.evaluate(context).fired

    def test_a_missing_value_is_unknown_not_false(self) -> None:
        """The distinction the whole missing-information feature rests on."""
        result = Comparison(ValueOf("potassium"), Operator.GT, 5.0).evaluate(_context())
        assert result.unknown
        assert not result.fired
        assert "potassium" in result.missing

    def test_a_unit_mismatch_is_refused_not_converted(self) -> None:
        """A silent conversion is how mmol/L is compared against mg/dL and looks plausible."""
        context = _context(
            _fact(FactKind.OBSERVATION, "Potassium", value=5.4, unit="mg/dL", effective=TODAY)
        )
        result = Comparison(ValueOf("potassium"), Operator.GT, 5.0, unit="mmol/L").evaluate(context)
        assert result.unknown
        assert "does not convert units" in result.explanation

    def test_an_undated_fact_cannot_satisfy_a_temporal_window(self) -> None:
        """Otherwise a rule about recent results fires on a result of unknown age."""
        context = _context(_fact(FactKind.OBSERVATION, "Creatinine", value=1.2))
        result = HasFact(kind=FactKind.OBSERVATION, name="creatinine", within_days=180).evaluate(
            context
        )
        assert result.unknown

    def test_conjunction_propagates_unknown(self) -> None:
        context = _context(_fact(FactKind.MEDICATION, "Lisinopril"))
        condition = AllOf(
            (
                HasFact(kind=FactKind.MEDICATION, name="lisinopril"),
                Comparison(ValueOf("potassium"), Operator.GT, 5.0),
            )
        )
        result = condition.evaluate(context)
        assert result.unknown
        assert not result.fired

    def test_a_definite_false_settles_a_conjunction(self) -> None:
        """Asking for the missing datum would be noise: it cannot change the outcome."""
        condition = AllOf(
            (
                HasFact(kind=FactKind.MEDICATION, name="warfarin"),
                Comparison(ValueOf("potassium"), Operator.GT, 5.0),
            )
        )
        result = condition.evaluate(_context())
        assert not result.unknown
        assert not result.satisfied

    def test_one_true_arm_settles_a_disjunction(self) -> None:
        context = _context(_fact(FactKind.MEDICATION, "Lisinopril"))
        condition = AnyOf(
            (
                HasFact(kind=FactKind.MEDICATION, name="lisinopril"),
                Comparison(ValueOf("potassium"), Operator.GT, 5.0),
            )
        )
        assert condition.evaluate(context).fired

    def test_negation_does_not_turn_unknown_into_true(self) -> None:
        """Otherwise absence of data becomes a positive clinical finding."""
        condition = Not(Comparison(ValueOf("potassium"), Operator.GT, 5.0))
        result = condition.evaluate(_context())
        assert result.unknown
        assert not result.fired

    def test_negation_of_a_confirmed_absence_is_true(self) -> None:
        result = Not(HasFact(kind=FactKind.MEDICATION, name="warfarin")).evaluate(_context())
        assert result.fired

    def test_a_single_point_is_not_a_trend(self) -> None:
        """Calling one reading "stable" is an assertion the data does not support."""
        context = _context(_fact(FactKind.OBSERVATION, "Potassium", value=5.4, effective=TODAY))
        result = Trend("potassium", TrendDirection.RISING).evaluate(context)
        assert result.unknown

    def test_a_rising_series_is_detected(self) -> None:
        context = _context(
            _fact(FactKind.OBSERVATION, "Potassium", value=4.1, effective=dt.date(2025, 9, 2)),
            _fact(FactKind.OBSERVATION, "Potassium", value=5.4, effective=dt.date(2026, 3, 14)),
        )
        assert Trend("potassium", TrendDirection.RISING).evaluate(context).fired


class TestKnowledgeLoading:
    def test_the_shipped_corpus_loads(self) -> None:
        base = load_knowledge_base(CORPUS)
        assert base.rules and base.interactions and base.risk_models and base.pathways

    def test_versioning_deactivates_a_superseded_rule(self) -> None:
        base = load_knowledge_base(CORPUS)
        assert len(base.active_rules(TODAY)) < len(base.rules)

    def test_a_rule_without_a_citation_is_refused(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        (tmp_path / "bad.yaml").write_text(
            "rules:\n  - id: r\n    version: '1'\n    title: t\n"
            "    when: {always: yes}\n    recommend: do it\n"
            "    severity: major\n    evidence_quality: established\n",
            encoding="utf-8",
        )
        with pytest.raises(KnowledgeError, match="citation is required"):
            load_knowledge_base(tmp_path)

    def test_an_unknown_key_is_refused(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """A misspelled `severity` would silently take a default — which for a
        contraindication means becoming an informational note."""
        (tmp_path / "bad.yaml").write_text(
            "rules:\n  - id: r\n    version: '1'\n    title: t\n"
            "    when: {always: yes}\n    recommend: do it\n"
            "    severty: contraindicated\n    severity: major\n"
            "    evidence_quality: established\n    citations: [src]\n",
            encoding="utf-8",
        )
        with pytest.raises(KnowledgeError, match="unknown key"):
            load_knowledge_base(tmp_path)

    def test_a_duplicate_rule_version_is_refused(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """Which one is active would otherwise depend on file order."""
        entry = (
            "  - id: r\n    version: '1'\n    title: t\n    when: {always: yes}\n"
            "    recommend: do it\n    severity: major\n"
            "    evidence_quality: established\n    citations: [src]\n"
        )
        (tmp_path / "dup.yaml").write_text(f"rules:\n{entry}{entry}", encoding="utf-8")
        with pytest.raises(KnowledgeError, match="more than once"):
            load_knowledge_base(tmp_path)

    def test_an_unsupported_operator_raises_rather_than_evaluating_false(self) -> None:
        """A rule that silently never fires is indistinguishable from one that works."""
        with pytest.raises(KnowledgeError, match="unsupported condition operator"):
            parse_condition({"whenever": {"kind": "medication"}})

    def test_a_condition_must_have_exactly_one_operator(self) -> None:
        with pytest.raises(KnowledgeError, match="exactly one operator"):
            parse_condition({"has": {"kind": "medication", "name": "x"}, "not": {}})


class TestRuleEngine:
    @pytest.fixture
    def engine(self):  # type: ignore[no-untyped-def]
        return build_rule_engine(load_knowledge_base(CORPUS))

    def test_a_matching_patient_fires_the_rule(self, engine) -> None:  # type: ignore[no-untyped-def]
        context = _context(
            _fact(FactKind.MEDICATION, "Lisinopril 10 mg"),
            _fact(FactKind.OBSERVATION, "Potassium", value=5.4, unit="mmol/L", effective=TODAY),
        )
        trace = engine.evaluate(context)
        assert any("hyperkalemia-on-raas-blockade" in o.rule.key for o in trace.fired)

    def test_the_trace_explains_rules_that_did_not_fire(self, engine) -> None:  # type: ignore[no-untyped-def]
        """A trace of only what fired cannot answer "why did it not warn me"."""
        trace = engine.evaluate(_context())
        assert len(trace.outcomes) > len(trace.fired)
        assert trace.render()

    def test_an_expired_rule_version_is_skipped(self, engine) -> None:  # type: ignore[no-untyped-def]
        trace = engine.evaluate(_context())
        assert any("0.9.0" in key for key in trace.skipped_inactive)

    def test_unevaluable_rules_become_missing_information(self, engine) -> None:  # type: ignore[no-untyped-def]
        context = _context(_fact(FactKind.MEDICATION, "Lisinopril 10 mg"))
        trace = engine.evaluate(context)
        assert trace.unevaluable
        assert "potassium" in trace.missing_information()

    def test_a_broken_rule_does_not_stop_the_others(self, engine) -> None:  # type: ignore[no-untyped-def]
        """A rule that silently errors is a rule that silently stopped protecting anyone."""
        from cip_decision.rules.engine import ClinicalRule

        class _Exploding:
            def evaluate(self, context):  # type: ignore[no-untyped-def]
                raise RuntimeError("boom")

            def describe(self) -> str:
                return "explodes"

        engine.register(
            ClinicalRule(
                rule_id="broken",
                version="1.0.0",
                title="Broken",
                condition=_Exploding(),
                recommendation_summary="never",
                severity=Severity.MINOR,
                evidence_quality=EvidenceQuality.POSSIBLE,
                citations=(Citation(source="Test"),),
            )
        )
        trace = engine.evaluate(_context())
        assert any(o.rule.rule_id == "broken" and o.unknown for o in trace.outcomes)
        assert len(trace.outcomes) > 1


class TestDrugIntelligence:
    @pytest.fixture
    def drugs(self):  # type: ignore[no-untyped-def]
        base = load_knowledge_base(CORPUS)
        return DrugIntelligence(
            interactions=base.interactions,
            drug_classes={
                "lisinopril": "ace-inhibitor",
                "spironolactone": "aldosterone-antagonist",
                "simvastatin": "statin",
                "atorvastatin": "statin",
                "clarithromycin": "macrolide",
            },
            cross_reactive_classes=frozenset({"beta-lactam"}),
        )

    def test_a_known_interaction_is_found(self, drugs) -> None:  # type: ignore[no-untyped-def]
        context = _context(
            _fact(FactKind.MEDICATION, "Lisinopril 10 mg"),
            _fact(FactKind.MEDICATION, "Spironolactone 25 mg"),
        )
        report = drugs.check(context)
        assert any(f.check is DrugCheckKind.INTERACTION for f in report.findings)

    def test_an_allergy_to_one_drug_does_not_contraindicate_its_class(self, drugs) -> None:  # type: ignore[no-untyped-def]
        """Regression (blocker): a Simvastatin allergy contraindicated Atorvastatin.

        Statins are not a cross-reactive allergy class; treating every class as one denies a
        patient a needed drug on no evidence.
        """
        context = _context(
            _fact(FactKind.ALLERGY, "Simvastatin", attributes={"class": "statin"}),
            _fact(FactKind.MEDICATION, "Atorvastatin 20 mg"),
        )
        report = drugs.check(context)
        assert not [f for f in report.findings if f.check is DrugCheckKind.ALLERGY]

    def test_a_declared_cross_reactive_class_does_match(self, drugs) -> None:  # type: ignore[no-untyped-def]
        context = _context(
            _fact(FactKind.ALLERGY, "Penicillin", attributes={"class": "beta-lactam"}),
            _fact(FactKind.MEDICATION, "Amoxicillin 500 mg", attributes={"class": "beta-lactam"}),
        )
        report = drugs.check(context)
        allergy = [f for f in report.findings if f.check is DrugCheckKind.ALLERGY]
        assert allergy
        assert allergy[0].severity is Severity.CONTRAINDICATED

    def test_an_exact_ingredient_allergy_always_matches(self, drugs) -> None:  # type: ignore[no-untyped-def]
        context = _context(
            _fact(FactKind.ALLERGY, "Simvastatin", attributes={"class": "statin"}),
            _fact(FactKind.MEDICATION, "Simvastatin 40 mg"),
        )
        assert [f for f in drugs.check(context).findings if f.check is DrugCheckKind.ALLERGY]

    def test_distinct_drug_pairs_do_not_share_a_concern(self, drugs) -> None:  # type: ignore[no-untyped-def]
        """Regression (blocker): a class-level entry matched two different pairs, and keying
        the dedup concern on the entry alone silently hid one genuine contraindication."""
        context = _context(
            _fact(FactKind.MEDICATION, "Simvastatin 40 mg"),
            _fact(FactKind.MEDICATION, "Atorvastatin 20 mg"),
            _fact(FactKind.MEDICATION, "Clarithromycin 500 mg"),
        )
        report = drugs.check(context)
        interactions = [f for f in report.findings if f.check is DrugCheckKind.INTERACTION]
        concerns = {f.to_recommendation(context).metadata["concern"] for f in interactions}
        assert len(concerns) == len(interactions)

    def test_duplicate_therapy_is_detected(self, drugs) -> None:  # type: ignore[no-untyped-def]
        context = _context(
            _fact(FactKind.MEDICATION, "Simvastatin 40 mg"),
            _fact(FactKind.MEDICATION, "Atorvastatin 20 mg"),
        )
        report = drugs.check(context)
        assert any(f.check is DrugCheckKind.DUPLICATE_THERAPY for f in report.findings)

    def test_absence_is_not_reported_as_safety(self, drugs) -> None:  # type: ignore[no-untyped-def]
        """The most consequential misreading available."""
        report = drugs.check(_context(_fact(FactKind.MEDICATION, "Paracetamol 500 mg")))
        assert not report.has_findings
        assert "not a determination that the combination is safe" in report.absence_statement()


class TestRiskScoring:
    @pytest.fixture
    def scorer(self) -> RiskScorer:
        return RiskScorer(build_risk_models(load_knowledge_base(CORPUS)))

    def test_a_model_is_not_scored_outside_its_population(self, scorer: RiskScorer) -> None:
        """Regression (blocker): CHA2DS2-VASc was computed and banded for patients without
        atrial fibrillation — a meaningless number that looks authoritative and could drive
        inappropriate anticoagulation."""
        context = _context(_fact(FactKind.CONDITION, "Hypertension"), age=80)
        result = scorer.score("cha2ds2-vasc", context)
        assert not result.applicable
        assert "atrial fibrillation" in result.inapplicable_reason
        assert "not scored" in result.explain().lower()

    def test_a_model_scores_inside_its_population(self, scorer: RiskScorer) -> None:
        context = _context(
            _fact(FactKind.CONDITION, "Atrial fibrillation"),
            _fact(FactKind.CONDITION, "Hypertension"),
            age=80,
        )
        result = scorer.score("cha2ds2-vasc", context)
        assert result.applicable
        assert result.score >= 3
        assert result.band == "high"

    def test_the_score_decomposes(self, scorer: RiskScorer) -> None:
        """A number a clinician cannot decompose is a number they cannot check."""
        context = _context(
            _fact(FactKind.CONDITION, "Atrial fibrillation"),
            _fact(FactKind.CONDITION, "Hypertension"),
            age=80,
        )
        result = scorer.score("cha2ds2-vasc", context)
        labels = {label for label, _, _ in result.contributing}
        assert "Hypertension" in labels
        assert result.absent

    def test_an_incomplete_score_is_a_lower_bound(self, scorer: RiskScorer) -> None:
        """Reporting it as a score understates risk for patients whose records are thin."""
        context = PatientContext(
            patient_id=PATIENT,
            tenant_id=TENANT,
            as_of=TODAY,
            age_years=None,
            facts=(_fact(FactKind.CONDITION, "Atrial fibrillation"),),
        )
        result = scorer.score("cha2ds2-vasc", context)
        assert not result.is_complete
        assert result.maximum_possible > result.score
        assert "lower bound" in result.explain()

    def test_applicable_results_filters(self, scorer: RiskScorer) -> None:
        context = _context(_fact(FactKind.CONDITION, "Hypertension"))
        assert len(scorer.applicable_results(context)) < len(scorer.score_all(context))


class TestCarePathways:
    @pytest.fixture
    def engine(self) -> PathwayEngine:
        return PathwayEngine(build_pathways(load_knowledge_base(CORPUS)))

    def test_an_untriggered_pathway_reports_why(self, engine: PathwayEngine) -> None:
        """ "The trigger did not fire because potassium is 4.1" is useful; silence is not."""
        context = _context(
            _fact(FactKind.OBSERVATION, "Potassium", value=4.1, unit="mmol/L", effective=TODAY)
        )
        applied = engine.apply("pw-hyperkalemia", context)
        assert not applied.triggered
        assert "4.1" in applied.trigger_reason

    def test_applicable_actions_resolve_against_the_patient(self, engine: PathwayEngine) -> None:
        context = _context(
            _fact(FactKind.OBSERVATION, "Potassium", value=5.4, unit="mmol/L", effective=TODAY),
            _fact(FactKind.MEDICATION, "Lisinopril 10 mg"),
        )
        applied = engine.apply("pw-hyperkalemia", context)
        assert applied.triggered
        ids = {a.action.action_id for a in applied.applicable}
        assert "review-raas" in ids

    def test_not_applicable_actions_are_retained_with_a_reason(self, engine: PathwayEngine) -> None:
        """ "We checked and it does not apply" is clinically different from silence."""
        context = _context(
            _fact(FactKind.OBSERVATION, "Potassium", value=5.4, unit="mmol/L", effective=TODAY)
        )
        applied = engine.apply("pw-hyperkalemia", context)
        skipped = [a for a in applied.actions if a.applicability is Applicability.NOT_APPLICABLE]
        assert skipped
        assert all(a.reason for a in skipped)

    def test_stages_render_in_clinical_order(self, engine: PathwayEngine) -> None:
        """A plan listing discharge before investigation is a plan nobody can follow."""
        context = _context(
            _fact(FactKind.OBSERVATION, "Potassium", value=5.4, unit="mmol/L", effective=TODAY),
            _fact(FactKind.MEDICATION, "Lisinopril 10 mg"),
        )
        stages = list(engine.apply("pw-hyperkalemia", context).by_stage())
        assert stages.index("investigation") < stages.index("discharge")

    def test_duplicate_action_ids_are_refused(self) -> None:
        from cip_decision.pathways.engine import CarePathway, PathwayAction, PathwayStage

        action = PathwayAction(action_id="a", title="t", stage=PathwayStage.INVESTIGATION)
        with pytest.raises(ValueError, match="duplicate action ids"):
            CarePathway(
                pathway_id="p",
                version="1",
                title="t",
                actions=(action, action),
                citations=(Citation(source="s"),),
            )


class TestSuppression:
    def test_a_contraindication_is_never_suppressed_by_a_floor(self) -> None:
        suppressor = Suppressor(policy=SuppressionPolicy(role=ClinicalRole.PRESCRIBER))
        result = suppressor.apply((_recommendation("c", severity=Severity.CONTRAINDICATED),))
        assert len(result.shown) == 1

    def test_a_contraindication_is_never_folded_by_the_ceiling(self) -> None:
        """A patient with nine contraindications sees nine, however unusual that is."""
        suppressor = Suppressor(policy=SuppressionPolicy(max_alerts=2))
        items = tuple(_recommendation(f"c{i}", severity=Severity.CONTRAINDICATED) for i in range(9))
        assert len(suppressor.apply(items).shown) == 9

    def test_the_severity_floor_is_role_dependent(self) -> None:
        """Role tailoring is the intervention the systematic reviews found most effective."""
        moderate = (_recommendation("m", severity=Severity.MODERATE),)
        prescriber = Suppressor(policy=SuppressionPolicy(role=ClinicalRole.PRESCRIBER))
        pharmacist = Suppressor(policy=SuppressionPolicy(role=ClinicalRole.PHARMACIST))
        assert not prescriber.apply(moderate).shown
        assert pharmacist.apply(moderate).shown

    def test_an_unknown_role_sees_everything(self) -> None:
        """Guessing a role wrongly in the restrictive direction hides a major interaction."""
        suppressor = Suppressor(policy=SuppressionPolicy(role=ClinicalRole.UNKNOWN))
        assert suppressor.apply((_recommendation("m", severity=Severity.MINOR),)).shown

    def test_a_shared_concern_deduplicates_and_keeps_supports(self) -> None:
        suppressor = Suppressor(policy=SuppressionPolicy(role=ClinicalRole.UNKNOWN))
        items = (
            _recommendation("a", concern="hyperkalemia", severity=Severity.MODERATE),
            _recommendation("b", concern="hyperkalemia", severity=Severity.MAJOR),
        )
        result = suppressor.apply(items)
        assert len(result.shown) == 1
        assert result.shown[0].severity is Severity.MAJOR
        assert "reaching the same conclusion" in result.shown[0].detail

    def test_recommendations_without_a_concern_never_merge(self) -> None:
        """Inferring that two alerts mean the same thing would occasionally merge two
        genuinely different concerns."""
        suppressor = Suppressor(policy=SuppressionPolicy(role=ClinicalRole.UNKNOWN))
        items = (_recommendation("a"), _recommendation("b"))
        assert len(suppressor.apply(items).shown) == 2

    def test_a_rejected_recommendation_does_not_return(self) -> None:
        """Rejecting it is information; repeating it discards that and spends attention."""
        suppressor = Suppressor(policy=SuppressionPolicy(role=ClinicalRole.UNKNOWN))
        item = _recommendation("a")
        suppressor.remember_override(item, reason="already-addressed")
        result = suppressor.apply((item,))
        assert not result.shown
        assert "previously rejected" in result.suppressed[0].suppression_reason

    def test_an_overridden_contraindication_still_returns(self) -> None:
        suppressor = Suppressor(policy=SuppressionPolicy(role=ClinicalRole.UNKNOWN))
        item = _recommendation("a", severity=Severity.CONTRAINDICATED)
        suppressor.remember_override(item, reason="already-addressed")
        assert suppressor.apply((item,)).shown

    def test_overflow_is_folded_into_a_discoverable_summary(self) -> None:
        """A suppressed alert must remain discoverable, not silently dropped."""
        suppressor = Suppressor(policy=SuppressionPolicy(role=ClinicalRole.UNKNOWN, max_alerts=3))
        items = tuple(_recommendation(f"r{i}", severity=Severity.MODERATE) for i in range(10))
        result = suppressor.apply(items)
        assert len(result.shown) == 3
        assert "folded into this summary" in result.summary_line

    def test_every_suppression_records_a_reason(self) -> None:
        """Suppression that cannot be audited is indistinguishable from a bug."""
        suppressor = Suppressor(policy=SuppressionPolicy(role=ClinicalRole.PRESCRIBER))
        result = suppressor.apply((_recommendation("m", severity=Severity.MINOR),))
        assert all(item.suppression_reason for item in result.suppressed)


class TestContradiction:
    def test_two_recommendations_pointing_away_do_not_conflict(self) -> None:
        """Regression (high): "avoid (allergy)" and "avoid (interaction)" are two reasons for
        one action, and an earlier detector reported them as a disagreement."""
        items = (
            _recommendation("a", subjects=("Simvastatin",), direction="away"),
            _recommendation("b", subjects=("Simvastatin",), direction="away"),
        )
        assert not detect_contradictions(items).has_conflicts

    def test_an_undeclared_direction_produces_no_conflict(self) -> None:
        """Inferring direction from prose produced false conflicts twice; a knowledge base
        that wants detection has to declare it."""
        items = (
            _recommendation("a", subjects=("Simvastatin",)),
            _recommendation("b", subjects=("Simvastatin",), direction="away"),
        )
        assert not detect_contradictions(items).has_conflicts

    def test_genuine_opposition_is_detected(self) -> None:
        items = (
            _recommendation("a", subjects=("Simvastatin",), direction="toward"),
            _recommendation("b", subjects=("Simvastatin",), direction="away"),
        )
        report = detect_contradictions(items)
        assert report.has_conflicts
        assert report.contradictions[0].subject == "Simvastatin"

    def test_different_subjects_never_conflict(self) -> None:
        items = (
            _recommendation("a", subjects=("Simvastatin",), direction="toward"),
            _recommendation("b", subjects=("Warfarin",), direction="away"),
        )
        assert not detect_contradictions(items).has_conflicts

    def test_direction_is_a_closed_set(self) -> None:
        assert Direction("toward") is Direction.TOWARD
        assert Direction("unstated") is Direction.UNSTATED


class TestApprovalWorkflow:
    @pytest.fixture
    def workflow(self) -> ApprovalWorkflow:
        return ApprovalWorkflow()

    def test_a_recommendation_starts_proposed(self, workflow: ApprovalWorkflow) -> None:
        record = workflow.submit(_recommendation("a"))
        assert record.state is ReviewState.PROPOSED

    def test_acceptance_requires_a_review_step(self, workflow: ApprovalWorkflow) -> None:
        """Requiring the claim first makes "somebody looked at this" a recorded fact."""
        workflow.submit(_recommendation("a"))
        with pytest.raises(ApprovalError, match="Cannot move"):
            workflow.accept("a", reviewer_id="dr-x")

    def test_the_full_path_to_acceptance_works(self, workflow: ApprovalWorkflow) -> None:
        workflow.submit(_recommendation("a"))
        workflow.claim("a", reviewer_id="dr-x")
        record = workflow.accept("a", reviewer_id="dr-x")
        assert record.state is ReviewState.ACCEPTED
        assert record.decided_by == "dr-x"

    def test_acceptance_requires_an_identified_human(self, workflow: ApprovalWorkflow) -> None:
        """There is no auto-accept path anywhere in this codebase."""
        workflow.submit(_recommendation("a"))
        with pytest.raises(ApprovalError, match="identified reviewer"):
            workflow.claim("a", reviewer_id="  ")

    def test_a_rejection_requires_a_reason(self, workflow: ApprovalWorkflow) -> None:
        """The reason is the only direct measurement of whether the knowledge base is right."""
        workflow.submit(_recommendation("a"))
        workflow.claim("a", reviewer_id="dr-x")
        with pytest.raises(ApprovalError, match="requires a reason"):
            workflow.reject("a", reviewer_id="dr-x", reason="")

    def test_a_terminal_state_cannot_be_reopened(self, workflow: ApprovalWorkflow) -> None:
        workflow.submit(_recommendation("a"))
        workflow.claim("a", reviewer_id="dr-x")
        workflow.reject("a", reviewer_id="dr-x", reason="already-addressed")
        with pytest.raises(ApprovalError, match="terminal"):
            workflow.accept("a", reviewer_id="dr-x")

    def test_stale_recommendations_expire(self, workflow: ApprovalWorkflow) -> None:
        """A recommendation about a three-day-old lab is stale, not pending."""
        workflow.submit(_recommendation("a"), now=dt.datetime(2026, 3, 1, tzinfo=dt.UTC))
        expired = workflow.expire_stale(now=dt.datetime(2026, 3, 20, tzinfo=dt.UTC))
        assert len(expired) == 1
        assert expired[0].state is ReviewState.EXPIRED

    def test_rejection_reasons_are_counted(self, workflow: ApprovalWorkflow) -> None:
        for identifier in ("a", "b"):
            workflow.submit(_recommendation(identifier))
            workflow.claim(identifier, reviewer_id="dr-x")
            workflow.reject(identifier, reviewer_id="dr-x", reason="already-addressed")
        assert workflow.rejection_reasons() == {"already-addressed": 2}

    def test_every_transition_is_audited(self) -> None:
        events: list[dict] = []
        workflow = ApprovalWorkflow(audit_sink=events.append)
        workflow.submit(_recommendation("a"))
        workflow.claim("a", reviewer_id="dr-x")
        workflow.accept("a", reviewer_id="dr-x")
        assert [e["action"] for e in events] == ["submitted", "under_review", "accepted"]


class TestCdsHooks:
    def test_a_card_conforms_to_the_specification(self) -> None:
        card = build_card(_recommendation("a", severity=Severity.MAJOR))
        payload = card.to_json()
        assert payload["indicator"] == "critical"
        assert len(payload["summary"]) <= 140
        assert payload["source"]["label"]

    def test_an_invalid_indicator_is_refused(self) -> None:
        with pytest.raises(ValueError, match="indicator must be"):
            Card(summary="s", indicator="urgent", source=Source(label="l"))

    def test_suggestions_require_a_selection_behavior(self) -> None:
        """A client otherwise cannot know whether the suggestions are mutually exclusive."""
        from cip_decision.hooks.cards import Suggestion

        with pytest.raises(ValueError, match="selectionBehavior"):
            Card(
                summary="s",
                indicator="info",
                source=Source(label="l"),
                suggestions=(Suggestion(label="do it"),),
            )

    def test_override_reasons_are_offered(self) -> None:
        """Structured, because free text cannot be counted."""
        card = build_card(_recommendation("a"))
        assert len(card.override_reasons) >= 4

    def test_the_deprecated_hook_says_so_in_discovery(self) -> None:
        """Shipping it silently would encode a stale integration into every deployment."""
        definition = service_definition(
            hook=HookType.MEDICATION_PRESCRIBE,
            service_id="x",
            title="t",
            description="d",
        )
        assert "DEPRECATED" in definition["description"]
        assert "order-select" in definition["description"]

    def test_current_hooks_are_not_marked_deprecated(self) -> None:
        definition = service_definition(
            hook=HookType.ORDER_SIGN, service_id="x", title="t", description="d"
        )
        assert "DEPRECATED" not in definition["description"]

    def test_a_delete_action_needs_a_resource_id(self) -> None:
        from cip_decision.hooks.cards import SuggestionAction

        with pytest.raises(ValueError, match="resourceId"):
            SuggestionAction(type="delete", description="remove")


class TestEvidenceGraph:
    def test_a_recommendation_is_explainable(self) -> None:
        graph = EvidenceGraph()
        graph.record(_recommendation("a"))
        assert graph.explain("a")

    def test_parallel_subjects_do_not_chain(self) -> None:
        """Regression (high): the graph asserted that one medication led to another, when
        both are parallel inputs to the same finding."""
        recommendation = Recommendation(
            id="r",
            kind=RecommendationKind.ALERT,
            summary="interaction",
            severity=Severity.MAJOR,
            evidence_quality=EvidenceQuality.ESTABLISHED,
            citations=(Citation(source="Test"),),
            provenance=(
                ProvenanceLink(kind="drug_check", identifier="ddi", label="check"),
                ProvenanceLink(kind="medication", identifier="Drug A", label="subject"),
                ProvenanceLink(kind="medication", identifier="Drug B", label="subject"),
            ),
        )
        graph = EvidenceGraph()
        graph.record(recommendation)
        paths = graph.explain("r")
        assert not any("Drug A" in p and "Drug B" in p for p in paths)

    def test_contribution_counts_by_kind(self) -> None:
        graph = EvidenceGraph()
        graph.record(_recommendation("a"))
        contribution = graph.contribution("a")
        assert contribution.get(NodeKind.RULE.value, 0) >= 1

    def test_an_unknown_recommendation_explains_to_nothing(self) -> None:
        assert EvidenceGraph().explain("nope") == ()


class TestModuleBoundaries:
    """The dependency rule for this service, enforced rather than documented."""

    LAYERS = {
        "domain": 0,
        "rules": 1,
        "knowledge": 2,
        "drugs": 2,
        "risk": 2,
        "pathways": 2,
        "factory": 3,
        "contradiction": 3,
        "suppression": 3,
        "evidence_graph": 3,
        "approval": 3,
        "hooks": 3,
        "smart": 3,
        "engine": 4,
        "workflow": 5,
        "evaluation": 5,
        "demo": 6,
    }

    def test_no_module_imports_upward(self) -> None:
        import re

        root = pathlib.Path(__file__).resolve().parents[2] / "services/decision/src/cip_decision"
        pattern = re.compile(r"^\s*from cip_decision\.(\w+)", re.M)

        violations: list[str] = []
        for path in root.rglob("*.py"):
            relative = path.relative_to(root)
            module = relative.parts[0] if len(relative.parts) > 1 else relative.stem
            own = self.LAYERS.get(module)
            if own is None:
                continue
            for imported in pattern.findall(path.read_text(encoding="utf-8")):
                other = self.LAYERS.get(imported)
                if other is None or imported == module:
                    continue
                if other >= own:
                    violations.append(
                        f"{relative} (layer {own}) imports {imported} (layer {other})"
                    )

        assert not violations, "upward or sideways imports:\n" + "\n".join(violations)

    def test_the_decision_service_does_not_import_other_services(self) -> None:
        """Phases 1-4 are consumed through their own entry points, never reached into."""
        import re

        root = pathlib.Path(__file__).resolve().parents[2] / "services/decision/src/cip_decision"
        forbidden = re.compile(r"\bcip_(ingestion|retrieval|copilot|gateway)\b")
        offenders = [
            str(p.relative_to(root))
            for p in root.rglob("*.py")
            if forbidden.search(p.read_text(encoding="utf-8"))
        ]
        assert not offenders, f"decision service reaches into other services: {offenders}"

    def test_no_clinical_content_is_hardcoded(self) -> None:
        """ADR-0019: clinical knowledge is data. A drug name in engine code means a rule
        that no clinical reviewer can see."""
        import re

        root = pathlib.Path(__file__).resolve().parents[2] / "services/decision/src/cip_decision"
        # Ingredient names that must appear only in the corpus, the demo, and tests.
        drugs = re.compile(
            r"\b(lisinopril|spironolactone|warfarin|simvastatin|clarithromycin|metformin)\b",
            re.I,
        )
        offenders: list[str] = []
        for path in root.rglob("*.py"):
            relative = path.relative_to(root)
            if relative.parts[0] in ("knowledge",) or relative.stem == "demo":
                continue
            text = path.read_text(encoding="utf-8")
            # Docstrings and comments may discuss examples; code may not.
            code = "\n".join(
                line for line in text.splitlines() if not line.lstrip().startswith("#")
            )
            for match in drugs.finditer(code):
                line = code[: match.start()].count("\n") + 1
                offenders.append(f"{relative}:{line} mentions {match.group(0)}")
        assert not offenders, "clinical content in engine code:\n" + "\n".join(offenders)


class TestResourceBounds:
    """Regressions for unbounded growth. 200 patients produced 600 approval records and
    3,000 graph edges before these bounds — a memory leak retaining clinical content tied to
    patient ids for the life of the process."""

    def test_closed_approval_records_are_evicted(self) -> None:
        workflow = ApprovalWorkflow(max_closed_records=10)
        for index in range(100):
            identifier = f"r{index}"
            workflow.submit(_recommendation(identifier))
            workflow.claim(identifier, reviewer_id="dr-x")
            workflow.reject(identifier, reviewer_id="dr-x", reason="already-addressed")
        assert len(workflow._records) <= 10

    def test_open_approval_records_are_never_evicted(self) -> None:
        """Dropping a pending review loses a clinical decision somebody is waiting on —
        categorically worse than the memory it saves."""
        workflow = ApprovalWorkflow(max_closed_records=1)
        for index in range(50):
            workflow.submit(_recommendation(f"open{index}"))
        assert len(workflow.open_records()) == 50

    def test_the_evidence_graph_is_bounded(self) -> None:
        graph = EvidenceGraph(max_recommendations=20)
        for index in range(200):
            graph.record(_recommendation(f"r{index}"))
        assert graph.node_count() < 200

    def test_a_recent_recommendation_stays_explainable(self) -> None:
        """Bounding must not make the newest recommendations unexplainable."""
        graph = EvidenceGraph(max_recommendations=20)
        for index in range(50):
            graph.record(_recommendation(f"r{index}"))
        assert graph.explain("r49")

    def test_override_memory_is_bounded(self) -> None:
        suppressor = Suppressor(
            policy=SuppressionPolicy(role=ClinicalRole.UNKNOWN), max_overrides=25
        )
        for index in range(200):
            suppressor.remember_override(_recommendation(f"r{index}"), reason="already-addressed")
        assert len(suppressor._overrides) <= 25


class TestEnginePolicyOverride:
    def test_a_per_call_policy_is_honoured(self) -> None:
        """Regression: `policy` was accepted and ignored, so an engine serving a prescriber
        and a pharmacist silently applied the wrong severity floor to one of them."""
        from cip_decision.engine import DecisionEngine
        from cip_decision.pathways.engine import PathwayEngine

        base = load_knowledge_base(CORPUS)
        engine = DecisionEngine(
            rules=build_rule_engine(base),
            drugs=DrugIntelligence(interactions=base.interactions),
            risk=RiskScorer(build_risk_models(base)),
            pathways=PathwayEngine(build_pathways(base)),
            suppressor=Suppressor(policy=SuppressionPolicy(role=ClinicalRole.PRESCRIBER)),
        )
        context = _context(
            _fact(FactKind.MEDICATION, "Ramipril 5 mg"),
            _fact(FactKind.CONDITION, "Hypertension"),
        )
        prescriber = engine.decide(context)
        reviewer = engine.decide(context, policy=SuppressionPolicy(role=ClinicalRole.REVIEWER))
        assert len(reviewer.recommendations) > len(prescriber.recommendations)


class TestDecisionEvaluation:
    """The evaluation harness itself.

    A harness that silently scores everything as passing is worse than none, so these assert
    that each metric actually fails when it should.
    """

    @staticmethod
    def _engine() -> DecisionEngine:
        base = load_knowledge_base(CORPUS)
        return DecisionEngine(
            rules=build_rule_engine(base),
            drugs=DrugIntelligence(interactions=base.interactions),
            risk=RiskScorer(build_risk_models(base)),
            pathways=PathwayEngine(build_pathways(base)),
            suppressor=Suppressor(policy=SuppressionPolicy(role=ClinicalRole.REVIEWER)),
        )

    @staticmethod
    def _hyperkalemic() -> PatientContext:
        return _context(
            _fact(FactKind.MEDICATION, "Lisinopril 10 mg"),
            _fact(FactKind.OBSERVATION, "Potassium", value=5.6, unit="mmol/L", effective=TODAY),
        )

    def test_an_empty_suite_reports_nothing_rather_than_dividing_by_zero(self) -> None:
        from cip_decision.evaluation import DecisionEvaluator

        base = load_knowledge_base(CORPUS)
        report = DecisionEvaluator(self._engine(), rules=build_rule_engine(base)).run([])
        assert report.case_count == 0

    def test_a_missing_expected_rule_fails_the_case(self) -> None:
        from cip_decision.evaluation import DecisionEvalCase, DecisionEvaluator

        base = load_knowledge_base(CORPUS)
        report = DecisionEvaluator(self._engine(), rules=build_rule_engine(base)).run(
            [
                DecisionEvalCase(
                    case_id="c",
                    context=_context(_fact(FactKind.CONDITION, "Hypertension")),
                    expected_rule_ids=frozenset({"hyperkalemia-on-raas-blockade"}),
                )
            ]
        )
        assert report.passed == 0
        assert report.rule_recall == 0.0

    def test_a_forbidden_rule_that_fires_is_a_safety_failure_not_a_precision_miss(self) -> None:
        """The distinction matters: a precision number averages away, a safety failure does
        not. Every fixed Phase 5 Blocker is expressed as a forbidden label."""
        from cip_decision.evaluation import DecisionEvalCase, DecisionEvaluator

        base = load_knowledge_base(CORPUS)
        report = DecisionEvaluator(self._engine(), rules=build_rule_engine(base)).run(
            [
                DecisionEvalCase(
                    case_id="c",
                    context=self._hyperkalemic(),
                    forbidden_rule_ids=frozenset({"hyperkalemia-on-raas-blockade"}),
                )
            ]
        )
        assert report.safety_failures
        assert report.false_positive_count == 1
        assert not report.is_clean

    def test_a_risk_model_scored_outside_its_population_is_a_safety_failure(self) -> None:
        """Regression for Blocker B1, expressed as an evaluation label."""
        from cip_decision.evaluation import DecisionEvalCase, DecisionEvaluator

        base = load_knowledge_base(CORPUS)
        report = DecisionEvaluator(self._engine(), rules=build_rule_engine(base)).run(
            [
                DecisionEvalCase(
                    case_id="c",
                    context=_context(_fact(FactKind.CONDITION, "Atrial fibrillation"), age=80),
                    forbidden_risk_models=frozenset({"cha2ds2-vasc"}),
                )
            ]
        )
        assert any("does not apply" in f for f in report.safety_failures)

    def test_rule_coverage_names_rules_no_case_exercises(self) -> None:
        """The metric that found a corpus rule with no case behind it."""
        from cip_decision.evaluation import DecisionEvalCase, DecisionEvaluator

        base = load_knowledge_base(CORPUS)
        report = DecisionEvaluator(self._engine(), rules=build_rule_engine(base)).run(
            [DecisionEvalCase(case_id="c", context=self._hyperkalemic())]
        )
        assert report.rule_coverage < 1.0
        assert "renal-impairment-metformin" in report.uncovered_rules

    def test_alert_burden_is_reported_alongside_accuracy(self) -> None:
        """Not combined into one score: a change that raises recall by alerting more would
        otherwise read as an improvement (ADR-0021)."""
        from cip_decision.evaluation import DecisionEvalCase, DecisionEvaluator

        base = load_knowledge_base(CORPUS)
        report = DecisionEvaluator(self._engine(), rules=build_rule_engine(base)).run(
            [DecisionEvalCase(case_id="c", context=self._hyperkalemic())]
        )
        assert report.mean_alerts_per_case > 0
        assert "alerts per case" in report.render()
        assert set(report.to_json()) >= {"rule_recall", "mean_alerts_per_case", "rule_coverage"}

    def test_a_case_expecting_silence_fails_when_the_engine_alerts(self) -> None:
        from cip_decision.evaluation import DecisionEvalCase, DecisionEvaluator

        base = load_knowledge_base(CORPUS)
        report = DecisionEvaluator(self._engine(), rules=build_rule_engine(base)).run(
            [
                DecisionEvalCase(
                    case_id="c", context=self._hyperkalemic(), expect_recommendations=False
                )
            ]
        )
        assert report.passed == 0

    def test_the_missing_information_label_is_checked(self) -> None:
        from cip_decision.evaluation import DecisionEvalCase, DecisionEvaluator

        base = load_knowledge_base(CORPUS)
        report = DecisionEvaluator(self._engine(), rules=build_rule_engine(base)).run(
            [
                DecisionEvalCase(
                    case_id="c",
                    context=_context(_fact(FactKind.MEDICATION, "Ramipril 5 mg")),
                    expected_missing=frozenset({"potassium"}),
                )
            ]
        )
        assert report.passed == 1

    def test_the_demo_suite_passes_and_covers_every_active_rule(self) -> None:
        """The shipped labelled suite. Coverage below 100% means the corpus contains knowledge
        no case has ever seen execute."""
        from cip_decision.demo import build_engine, labelled_cases
        from cip_decision.evaluation import DecisionEvaluator

        engine, base = build_engine()
        report = DecisionEvaluator(engine, rules=build_rule_engine(base)).run(labelled_cases())
        assert report.is_clean, report.render()
        assert report.rule_coverage == 1.0
        assert report.explanation_completeness == 1.0
