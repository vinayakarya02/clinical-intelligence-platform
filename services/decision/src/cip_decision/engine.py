"""The clinical decision engine.

Sequences the pipeline in docs/architecture/09-clinical-decision-intelligence.md: assemble →
evaluate → check → score → rank → detect → suppress → explain → gate.

Deterministic end to end. No model participates in a decision
(docs/design/adr-0022-deterministic-decisions.md); the same patient facts and the same
knowledge base produce the same recommendations, in the same order, every time. That is what
makes a knowledge base clinically reviewable — a reviewer can reason about what it will do.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from cip_core.logging import get_logger
from cip_decision.approval.workflow import ApprovalWorkflow
from cip_decision.contradiction import ContradictionReport, detect_contradictions
from cip_decision.domain import PatientContext, Recommendation
from cip_decision.drugs.intelligence import DrugIntelligence, DrugReport
from cip_decision.evidence_graph.graph import EvidenceGraph
from cip_decision.pathways.engine import AppliedPathway, PathwayEngine
from cip_decision.risk.scoring import RiskResult, RiskScorer
from cip_decision.rules.engine import RuleEngine, RuleTrace
from cip_decision.suppression import SuppressionPolicy, SuppressionResult, Suppressor

__all__ = ["DecisionEngine", "DecisionResult"]

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class DecisionResult:
    """Everything one decision run produced."""

    recommendations: tuple[Recommendation, ...] = ()
    suppressed: tuple[Recommendation, ...] = ()
    rule_trace: RuleTrace = field(default_factory=RuleTrace)
    drug_report: DrugReport = field(default_factory=DrugReport)
    risk_results: tuple[RiskResult, ...] = ()
    pathways: tuple[AppliedPathway, ...] = ()
    contradictions: ContradictionReport = field(default_factory=ContradictionReport)
    missing_information: tuple[str, ...] = ()
    summary_line: str = ""
    duration_ms: float = 0.0

    @property
    def has_recommendations(self) -> bool:
        return bool(self.recommendations)

    def absence_statement(self) -> str:
        """What an empty result means.

        The most consequential misreading available. Absence of a recommendation means the
        knowledge base had no rule that fired — not that there is no concern, and certainly
        not that the situation is safe.
        """
        parts = [
            f"{len(self.rule_trace.outcomes)} rule(s) were evaluated and "
            f"{len(self.rule_trace.fired)} fired.",
            self.drug_report.absence_statement(),
        ]
        if self.missing_information:
            parts.append(
                "Some rules could not be evaluated for want of: "
                + ", ".join(self.missing_information)
            )
        return " ".join(parts)

    def to_json(self) -> dict[str, Any]:
        return {
            "recommendations": [
                {
                    "id": r.id,
                    "summary": r.summary,
                    "severity": str(r.severity),
                    "evidence_quality": str(r.evidence_quality),
                    "priority": r.priority,
                    "state": str(r.state),
                }
                for r in self.recommendations
            ],
            "suppressed": [{"id": r.id, "reason": r.suppression_reason} for r in self.suppressed],
            "rules": self.rule_trace.to_json(),
            "drug_findings": len(self.drug_report.findings),
            "risk": [r.to_json() for r in self.risk_results],
            "pathways": [p.to_json() for p in self.pathways],
            "contradictions": [c.render() for c in self.contradictions.contradictions],
            "missing_information": list(self.missing_information),
            "duration_ms": round(self.duration_ms, 3),
        }


class DecisionEngine:
    """Runs the full clinical decision pipeline for one patient."""

    def __init__(
        self,
        *,
        rules: RuleEngine,
        drugs: DrugIntelligence,
        risk: RiskScorer,
        pathways: PathwayEngine,
        suppressor: Suppressor | None = None,
        approvals: ApprovalWorkflow | None = None,
        graph: EvidenceGraph | None = None,
    ) -> None:
        self._rules = rules
        self._drugs = drugs
        self._risk = risk
        self._pathways = pathways
        self._suppressor = suppressor or Suppressor()
        self._approvals = approvals or ApprovalWorkflow()
        self._graph = graph or EvidenceGraph()

    @property
    def approvals(self) -> ApprovalWorkflow:
        return self._approvals

    @property
    def evidence_graph(self) -> EvidenceGraph:
        return self._graph

    def decide(
        self, context: PatientContext, *, policy: SuppressionPolicy | None = None
    ) -> DecisionResult:
        """Run the pipeline.

        A per-call ``policy`` overrides the constructor's. It is honoured rather than ignored
        because the same engine serves a prescriber and a pharmacist, and silently applying
        the wrong severity floor is a safety-relevant lie about what was shown.
        """
        started = time.perf_counter()

        trace = self._rules.evaluate(context)
        rule_recommendations = RuleEngine.to_recommendations(trace, context)

        drug_report = self._drugs.check(context)
        drug_recommendations = tuple(
            finding.to_recommendation(context) for finding in drug_report.by_severity()
        )

        risk_results = self._risk.score_all(context)
        applied_pathways = self._pathways.apply_triggered(context)

        candidates = (*rule_recommendations, *drug_recommendations)

        # Contradictions are detected *before* suppression, so a conflict between a shown and
        # a suppressed recommendation is still found. Detecting afterwards would let
        # suppression hide one half of a conflict and make the other look unopposed.
        contradictions = detect_contradictions(candidates)

        suppressor = Suppressor(policy=policy) if policy is not None else self._suppressor
        suppression: SuppressionResult = suppressor.apply(candidates)

        for recommendation in suppression.shown:
            self._graph.record(recommendation)
            self._approvals.submit(recommendation)

        result = DecisionResult(
            recommendations=suppression.shown,
            suppressed=suppression.suppressed,
            rule_trace=trace,
            drug_report=drug_report,
            risk_results=risk_results,
            pathways=applied_pathways,
            contradictions=contradictions,
            missing_information=trace.missing_information(),
            summary_line=suppression.summary_line,
            duration_ms=(time.perf_counter() - started) * 1000,
        )
        _log.info(
            "decision.completed",
            shown=len(result.recommendations),
            suppressed=len(result.suppressed),
            contradictions=len(contradictions.contradictions),
            duration_ms=round(result.duration_ms, 2),
        )
        return result
