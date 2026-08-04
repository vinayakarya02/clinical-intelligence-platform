"""Pipeline stages.

Each stage is ``async (CopilotState, StageDeps) -> CopilotState``. They do not call each
other; the orchestrator sequences them. That is what makes every stage testable with a
hand-built state and no orchestrator, and it is why the trace is complete — a stage cannot run
without appending its record.

Stages check ``state.halted`` and pass through when set, so a safety block or an approval
suspension short-circuits the remainder without every stage re-deriving whether it should run.
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any

from cip_copilot.domain import (
    ConfidenceBreakdown,
    CopilotState,
    Evidence,
    EvidenceKind,
    PendingApproval,
    StageRecord,
)
from cip_copilot.explanations.explainer import build_explanation
from cip_copilot.llm.base import GenerationRequest, LanguageModel, LanguageModelError
from cip_copilot.memory.session import EntityMention, MemoryStore, resolve_references
from cip_copilot.planner.plan import Plan, PlanValidationError, StepKind, validate_plan
from cip_copilot.planner.rule_planner import Planner
from cip_copilot.prompts.catalog import PromptCatalog
from cip_copilot.reasoning.aggregator import (
    aggregate_evidence,
    build_claims,
    evidence_recency,
)
from cip_copilot.safety.detectors import assess_safety, evidence_agreement
from cip_copilot.tools.base import ToolContext, ToolError, ToolRegistry
from cip_copilot.validation.verifier import verify_answer_text, verify_claims
from cip_core.logging import get_logger
from cip_core.tenancy import TenantContext

__all__ = [
    "StageDeps",
    "stage_aggregate",
    "stage_execute",
    "stage_generate",
    "stage_plan",
    "stage_reason",
    "stage_reflect",
    "stage_remember",
    "stage_validate",
]

_log = get_logger(__name__)

#: Word-ish tokens for coverage scoring. Keeps clinical forms like ``mg/dl`` and ``5.4``
#: intact rather than splitting them into meaningless fragments.
_CONTENT_TERM = re.compile(r"[a-z0-9]+(?:[./-][a-z0-9]+)*")

#: Question words that no clinical record will ever contain. Counting them as uncovered
#: made coverage a measure of phrasing rather than of whether the question was answered:
#: "concerning", "given", and "current" cannot appear in a medication row, so every
#: naturally-worded question scored badly however well it was answered.
_COVERAGE_STOPWORDS = frozenset(
    {
        "what",
        "which",
        "when",
        "where",
        "does",
        "should",
        "would",
        "could",
        "there",
        "about",
        "given",
        "current",
        "currently",
        "concerning",
        "have",
        "with",
        "from",
        "this",
        "that",
        "their",
        "they",
        "them",
        "please",
        "tell",
        "show",
        "list",
        "most",
        "recent",
        "latest",
        "patient",
        "patients",
    }
)


@dataclass(frozen=True, slots=True)
class StageDeps:
    """Everything the stages need, injected.

    A single frozen container rather than eight constructor arguments threaded through every
    stage: adding a dependency then touches one type instead of every signature, and a test
    can build one with only the fields the stage under test uses.
    """

    registry: ToolRegistry
    planner: Planner
    memory: MemoryStore
    catalog: PromptCatalog
    model: LanguageModel
    scopes: frozenset[str] = frozenset({"documents:read", "patients:read", "reference:read"})
    confidence_threshold: float = 0.45
    max_evidence: int = 12
    max_output_tokens: int = 700
    approvals: frozenset[str] = frozenset()
    """Step ids a human has already approved. Present so a resumed run can proceed past the
    step that suspended it."""


def _record(stage: str, started: float, summary: str, **details: Any) -> StageRecord:
    return StageRecord(
        stage=stage,
        duration_ms=(time.perf_counter() - started) * 1000,
        summary=summary,
        details=details,
    )


async def stage_remember(state: CopilotState, deps: StageDeps) -> CopilotState:
    """Resolve references against session memory."""
    started = time.perf_counter()
    question = state.question

    memory = deps.memory.get(tenant_id=question.tenant_id, session_id=question.session_id)
    resolution = resolve_references(question.text, memory, explicit_patient=question.patient_id)

    if question.patient_id is not None:
        deps.memory.note_entity(
            tenant_id=question.tenant_id,
            session_id=question.session_id,
            mention=EntityMention(
                kind="patient",
                value=str(question.patient_id),
                display="current patient",
                last_turn=len(memory.working) + 1,
            ),
        )

    if resolution.unresolved:
        # An unresolved pronoun is the one case with no acceptable guess: answering about the
        # wrong patient is worse than any amount of asking.
        return state.advanced(
            _record(
                "remember",
                started,
                f"cannot resolve {', '.join(resolution.unresolved)}",
                unresolved=list(resolution.unresolved),
            ),
            halted="clarification",
            draft_text=(
                f"Which patient is this about? '{resolution.unresolved[0]}' has no referent "
                "in this conversation."
            ),
        )

    summary = "no references to resolve"
    if resolution.substitutions:
        summary = f"resolved {', '.join(resolution.substitutions)}"
    elif resolution.subject_carried:
        summary = "carried the previous question's subject forward"

    return state.advanced(
        _record(
            "remember",
            started,
            summary,
            working_turns=len(memory.working),
            episodic=len(memory.episodic),
        ),
        resolved_text=resolution.text,
    )


async def stage_plan(state: CopilotState, deps: StageDeps) -> CopilotState:
    """Produce and validate a plan."""
    if state.halted:
        return state
    started = time.perf_counter()

    plan = deps.planner.plan(
        state.question,
        resolved_text=state.resolved_text or state.question.text,
        available=deps.registry.names(),
    )

    if plan.needs_clarification:
        return state.advanced(
            _record("plan", started, "needs clarification", intent=plan.intent),
            plan=plan,
            halted="clarification",
            draft_text=plan.needs_clarification,
        )

    try:
        validate_plan(plan, registry=deps.registry)
    except PlanValidationError as exc:
        # A malformed plan is a planner bug, not a clinical outcome. Halting keeps a
        # partially-executed plan from reading PHI on the way to failing.
        return state.advanced(
            _record("plan", started, f"plan rejected: {exc}", intent=plan.intent),
            plan=plan,
            halted="invalid_plan",
            draft_text="The assistant could not construct a valid plan for this question.",
        )

    return state.advanced(
        _record(
            "plan",
            started,
            f"planned {len(plan.steps)} step(s): {plan.describe()}" if plan.steps else "no steps",
            intent=plan.intent,
            confidence=plan.confidence,
            rationale=plan.rationale,
        ),
        plan=plan,
    )


async def stage_execute(state: CopilotState, deps: StageDeps) -> CopilotState:
    """Run the plan's steps, isolating failures."""
    if state.halted:
        return state
    started = time.perf_counter()

    plan: Plan | None = state.plan
    if plan is None or plan.is_empty:
        return state.advanced(_record("execute", started, "nothing to execute"))

    context = ToolContext(
        tenant_id=state.question.tenant_id,
        scopes=deps.scopes,
        patient_id=state.question.patient_id,
        request_id=state.question.request_id,
    )

    for step in plan.steps:
        if step.kind is not StepKind.TOOL:
            continue
        spec = deps.registry.get(step.capability).spec
        if spec.requires_approval and step.step_id not in deps.approvals:
            return state.advanced(
                _record("execute", started, f"awaiting approval for {step.capability}"),
                halted="approval",
                pending_approval=PendingApproval(
                    step_id=step.step_id,
                    tool_name=step.capability,
                    reason=f"{step.capability} requires human approval before it runs",
                    arguments=dict(step.arguments),
                ),
            )

    # Steps whose arguments depend on an earlier step run after it; the rest run concurrently.
    # `drug_interaction_check` needs the medication list, so it is the one dependent step
    # today — declared here rather than inferred, because an inferred dependency graph would
    # be a scheduler nobody asked for.
    dependent = {"drug_interaction_check"}
    independent = [s for s in plan.steps if s.capability not in dependent]
    deferred = [s for s in plan.steps if s.capability in dependent]

    groups: list[tuple[str, tuple[Evidence, ...]]] = []
    degraded: list[str] = []
    tool_data: dict[str, Any] = dict(state.tool_data)

    async def _run(step: Any) -> tuple[str, Any]:
        result = await deps.registry.invoke(step.capability, dict(step.arguments), context=context)
        return step.capability, result

    outcomes = await asyncio.gather(*(_run(step) for step in independent), return_exceptions=True)
    for step, outcome in zip(independent, outcomes, strict=True):
        if isinstance(outcome, BaseException):
            degraded.append(step.capability)
            _log.warning(
                "copilot.step_failed",
                step=step.step_id,
                capability=step.capability,
                error=type(outcome).__name__,
            )
            continue
        capability, result = outcome
        groups.append((capability, result.evidence))
        if result.data:
            tool_data[capability] = result.data

    medications = _medication_names(tool_data)
    for step in deferred:
        arguments = dict(step.arguments)
        if step.capability == "drug_interaction_check":
            # Merge, never replace: the step already carries the question text so the tool
            # can resolve drugs named in it, and overwriting that with an empty lookup
            # result would silence the check whenever no medication step ran.
            arguments["medications"] = sorted({*(arguments.get("medications") or []), *medications})
        try:
            result = await deps.registry.invoke(step.capability, arguments, context=context)
        except ToolError as exc:
            degraded.append(step.capability)
            _log.warning("copilot.step_failed", step=step.step_id, error=str(exc))
            continue
        groups.append((step.capability, result.evidence))
        if result.data:
            tool_data[step.capability] = result.data

    interaction = tool_data.get("drug_interaction_check", {})
    if interaction.get("pairs"):
        tool_data["interaction_pairs"] = interaction["pairs"]

    collected = tuple(item for _name, items in groups for item in items)
    return state.advanced(
        _record(
            "execute",
            started,
            f"ran {len(groups)} capability call(s), collected {len(collected)} item(s)",
            capabilities=[name for name, _ in groups],
            degraded=degraded,
        ),
        evidence=collected,
        tool_data=tool_data,
    )


def _medication_names(tool_data: dict[str, Any]) -> list[str]:
    """Medication names an interaction check should consider."""
    payload = tool_data.get("medication_lookup") or {}
    return [str(m).split()[0].lower() for m in payload.get("medications", []) if str(m).strip()]


async def stage_aggregate(state: CopilotState, deps: StageDeps) -> CopilotState:
    """Deduplicate and rank the collected evidence."""
    if state.halted:
        return state
    started = time.perf_counter()

    result = aggregate_evidence(
        [("collected", state.evidence)], limit=min(deps.max_evidence, state.question.max_evidence)
    )
    return state.advanced(
        _record(
            "aggregate",
            started,
            f"kept {len(result.evidence)} of {len(state.evidence)} item(s)",
            duplicates=result.dropped_duplicates,
            over_budget=result.dropped_over_budget,
            by_kind=result.by_kind,
        ),
        evidence=result.evidence,
    )


async def stage_reason(state: CopilotState, deps: StageDeps) -> CopilotState:
    """Turn evidence into claims."""
    if state.halted:
        return state
    started = time.perf_counter()

    claims = build_claims(state.resolved_text or state.question.text, state.evidence)
    return state.advanced(
        _record("reason", started, f"formed {len(claims)} claim(s)", claims=len(claims)),
        claims=claims,
    )


async def stage_reflect(state: CopilotState, deps: StageDeps) -> CopilotState:
    """Verify every claim against the evidence it cites."""
    if state.halted:
        return state
    started = time.perf_counter()

    report = verify_claims(state.claims, state.evidence)
    confidence = _score_confidence(state, report)

    return state.advanced(
        _record(
            "reflect",
            started,
            f"{len(report.verified)} of {len(state.claims)} claim(s) verified",
            pass_rate=report.pass_rate,
            hallucination_rate=report.hallucination_rate,
            rejected=list(report.rejection_notes()),
        ),
        claims=report.verified,
        confidence=confidence,
    )


def _score_confidence(state: CopilotState, report: Any) -> ConfidenceBreakdown:
    """Compute the confidence components from the verified state."""
    verified = report.verified
    evidence = state.evidence

    strength = (
        round(sum(c.support.weight for c in verified) / len(verified), 4) if verified else 0.0
    )
    # Tokenised, not split on whitespace: a bare `.split()` leaves "medications?" attached to
    # its question mark, which then matches nothing in the evidence and understates coverage
    # on every question that ends in one — which is all of them.
    question_terms = {
        term
        for term in _CONTENT_TERM.findall((state.resolved_text or state.question.text).lower())
        if len(term) > 3 and term not in _COVERAGE_STOPWORDS
    }
    # A facet counts as covered if the evidence mentions it *or* a capability was run to
    # address it: a question about "medications" is answered by `medication_lookup` even
    # though that word never appears in a medication row.
    capabilities = " ".join(
        cap
        for record in state.trace
        if record.stage == "execute"
        for cap in record.details.get("capabilities", ())
    ).replace("_", " ")
    haystack = " ".join(item.content.lower() for item in evidence) + " " + capabilities
    covered = {term for term in question_terms if term in haystack or term[:-1] in haystack}
    coverage = round(len(covered) / len(question_terms), 4) if question_terms else 0.0

    quality_by_type = {"guideline": 1.0, "discharge_summary": 0.9, "lab_report": 0.9}
    quality = (
        round(
            sum(quality_by_type.get(item.document_type or "", 0.7) for item in evidence)
            / len(evidence),
            4,
        )
        if evidence
        else 0.0
    )

    return ConfidenceBreakdown(
        evidence_strength=strength,
        agreement=evidence_agreement(evidence),
        coverage=coverage,
        recency=evidence_recency(evidence),
        source_quality=quality,
        verification=report.pass_rate,
    )


async def stage_generate(state: CopilotState, deps: StageDeps) -> CopilotState:
    """Compose prose from the verified claims."""
    if state.halted:
        return state
    started = time.perf_counter()

    if not state.claims:
        return state.advanced(
            _record("generate", started, "no verified claims to state"),
            draft_text="",
        )

    system, versions = deps.catalog.compose_system(session_id=state.question.session_id)
    numbered = {item.id: index for index, item in enumerate(state.evidence, start=1)}
    body = "\n".join(
        f"- {claim.statement} "
        + " ".join(f"[{numbered[eid]}]" for eid in claim.evidence_ids if eid in numbered)
        for claim in state.claims
    )

    graph_section = ""
    graph_items = [item for item in state.evidence if item.kind is EvidenceKind.GRAPH_RELATIONSHIP]
    if graph_items:
        rendered, graph_version = deps.catalog.compose_task(
            "copilot_graph_section",
            {"graph_evidence": "\n".join(f"- {item.content}" for item in graph_items)},
            session_id=state.question.session_id,
        )
        graph_section = rendered
        versions["copilot_graph_section"] = graph_version

    # Rendered from the registry, never built inline: the task prompt is where claim text —
    # which contains verbatim passages from user-uploaded documents — meets the model, so it
    # needs the same versioning and the same injection boundary as every other prompt.
    user, task_version = deps.catalog.compose_task(
        "copilot_answer",
        {
            "question": state.resolved_text or state.question.text,
            "claims": body,
            "graph_section": graph_section,
        },
        session_id=state.question.session_id,
    )
    versions["copilot_answer"] = task_version

    try:
        response = await deps.model.complete(
            GenerationRequest(system=system, user=user, max_output_tokens=deps.max_output_tokens)
        )
    except LanguageModelError as exc:
        # No model means no answer, not a guessed one. The evidence and claims survive in the
        # state, so the caller still gets an explanation of what was found.
        return state.advanced(
            _record("generate", started, f"generation unavailable: {exc}"),
            halted="no_model",
            draft_text="",
            prompt_versions=versions,
        )

    if response.was_truncated:
        # A truncated clinical answer can end just before a caveat and read as a complete
        # one. Withholding it is the safe outcome; the evidence and claims survive on the
        # state, so the caller still sees what was found.
        return state.advanced(
            _record(
                "generate",
                started,
                "generation hit the output limit and was withheld as incomplete",
                model=response.model_key,
                truncated=True,
            ),
            halted="truncated",
            draft_text="",
            usage=state.usage + response.usage,
            prompt_versions=versions,
        )

    return state.advanced(
        _record(
            "generate",
            started,
            f"composed {len(response.text.split())} word(s)",
            model=response.model_key,
            truncated=False,
        ),
        draft_text=response.text,
        usage=state.usage + response.usage,
        prompt_versions=versions,
    )


async def stage_validate(state: CopilotState, deps: StageDeps) -> CopilotState:
    """Safety gate, then confidence gate."""
    started = time.perf_counter()

    report = assess_safety(
        question=state.question.text,
        evidence=state.evidence,
        claims=state.claims,
        answer_text=state.draft_text,
        tool_data=state.tool_data,
    )

    if state.halted:
        return state.advanced(
            _record("validate", started, f"halted earlier: {state.halted}"),
            safety_findings=report.findings,
        )

    if report.blocks:
        blocking = report.blocking()[0]
        return state.advanced(
            _record("validate", started, f"blocked: {blocking.code}", code=blocking.code),
            halted="safety",
            safety_findings=report.findings,
        )

    ok, problems = verify_answer_text(state.draft_text, state.claims, state.evidence)
    if not ok:
        return state.advanced(
            _record("validate", started, f"answer text failed: {problems[0]}"),
            halted="unverified_text",
            safety_findings=report.findings,
        )

    if state.confidence.score < deps.confidence_threshold:
        return state.advanced(
            _record(
                "validate",
                started,
                f"confidence {state.confidence.score:.2f} below {deps.confidence_threshold:.2f}",
            ),
            halted="low_confidence",
            safety_findings=report.findings,
        )

    return state.advanced(
        _record("validate", started, f"passed with confidence {state.confidence.score:.2f}"),
        safety_findings=report.findings,
    )


def explanation_for(state: CopilotState, dropped: tuple[str, ...] = ()) -> Any:
    """Build the explanation from a finished state."""
    return build_explanation(
        evidence=state.evidence,
        claims=state.claims,
        confidence=state.confidence,
        trace=state.trace,
        dropped=dropped,
    )


def service_context(tenant_id: uuid.UUID) -> TenantContext:
    """Tenant context for calling into the retrieval service."""
    return TenantContext.for_service(tenant_id)
