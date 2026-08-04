"""The copilot orchestrator.

Sequences the stages and turns the final state into an :class:`Answer`. It is the only module
that depends on everything else, and nothing depends on it — the top of the dependency order
in docs/design/adr-0008-copilot-module-boundaries.md.

The sequence is fixed. That is the point of ADR-0009: control flow is code, so the same
question takes the same path every time, every path is reachable in a test, and the decision
to read PHI is made by a validated plan rather than inside a model.

Suspension for human approval works by returning the state. ``resume`` takes it back with the
approval recorded and re-enters the pipeline from the top — replay rather than a saved
program counter, because the stages are pure and replaying them is cheaper than a resumable
coroutine and far easier to reason about.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace

from cip_copilot.agents.stages import (
    StageDeps,
    stage_aggregate,
    stage_execute,
    stage_generate,
    stage_plan,
    stage_reason,
    stage_reflect,
    stage_remember,
    stage_validate,
)
from cip_copilot.domain import (
    Answer,
    CopilotQuestion,
    CopilotState,
    ResponseMode,
    StageRecord,
)
from cip_copilot.explanations.explainer import Explanation, build_explanation
from cip_copilot.memory.session import Turn
from cip_core.logging import get_logger

__all__ = ["ClinicalCopilot", "CopilotResult"]

_log = get_logger(__name__)

Stage = Callable[[CopilotState, StageDeps], Awaitable[CopilotState]]

#: The fixed pipeline. Ordered, not configurable: a deployment that could reorder these could
#: put generation before verification, which is the one arrangement that must be impossible.
_PIPELINE: tuple[tuple[str, Stage], ...] = (
    ("remember", stage_remember),
    ("plan", stage_plan),
    ("execute", stage_execute),
    ("aggregate", stage_aggregate),
    ("reason", stage_reason),
    ("reflect", stage_reflect),
    ("generate", stage_generate),
    ("validate", stage_validate),
)

#: How a halt maps to a response mode. Every halt reason must appear here; the orchestrator
#: raises on an unmapped one rather than defaulting, because a silent default would turn a
#: new failure mode into a confident answer.
_HALT_MODES: dict[str, ResponseMode] = {
    "clarification": ResponseMode.CLARIFICATION,
    "invalid_plan": ResponseMode.UNCERTAIN,
    "approval": ResponseMode.NEEDS_APPROVAL,
    "safety": ResponseMode.BLOCKED,
    "unverified_text": ResponseMode.UNCERTAIN,
    "low_confidence": ResponseMode.UNCERTAIN,
    "no_model": ResponseMode.UNCERTAIN,
    "truncated": ResponseMode.UNCERTAIN,
    "approval_denied": ResponseMode.BLOCKED,
}


@dataclass(frozen=True, slots=True)
class CopilotResult:
    """One turn's answer, its explanation, and the state that produced it."""

    answer: Answer
    explanation: Explanation
    state: CopilotState
    """Retained so a suspended run can be resumed and so a reviewer can inspect the exact
    inputs to every stage."""

    @property
    def needs_approval(self) -> bool:
        return self.answer.mode is ResponseMode.NEEDS_APPROVAL


class ClinicalCopilot:
    """Runs the clinical reasoning pipeline for one question at a time."""

    def __init__(self, deps: StageDeps) -> None:
        self._deps = deps

    async def ask(self, question: CopilotQuestion) -> CopilotResult:
        """Answer one question."""
        started = time.perf_counter()
        state = CopilotState(question=question)
        state = await self._run(state, self._deps)

        result = self._finalise(state)
        self._remember_turn(question, result)

        _log.info(
            "copilot.answered",
            mode=str(result.answer.mode),
            confidence=result.answer.confidence.score,
            claims=len(result.answer.claims),
            evidence=len(result.answer.cited_evidence()),
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
        )
        return result

    async def resume(self, result: CopilotResult, *, approved: bool) -> CopilotResult:
        """Continue a run that suspended for human approval.

        Replays the pipeline with the decision recorded rather than resuming a coroutine.
        The stages are pure, so a replay produces the same intermediate states — and the
        approval is in the trace, which a resumable coroutine would not give for free.
        """
        pending = result.answer.pending_approval
        if pending is None:
            raise ValueError("This result is not awaiting approval")

        decision = StageRecord(
            stage="approval",
            duration_ms=0.0,
            summary=f"{pending.tool_name} {'approved' if approved else 'denied'} by a human",
            details={"step_id": pending.step_id, "approved": approved},
        )

        if not approved:
            state = replace(
                result.state,
                trace=(*result.state.trace, decision),
                halted="approval_denied",
                pending_approval=None,
                draft_text=(
                    f"The {pending.tool_name} step was not approved, so no answer was produced."
                ),
            )
            return self._finalise(state, mode_override=ResponseMode.BLOCKED)

        deps = replace(self._deps, approvals=self._deps.approvals | {pending.step_id})
        fresh = CopilotState(question=result.state.question, trace=(decision,))
        state = await self._run(fresh, deps)
        return self._finalise(state)

    @staticmethod
    async def _run(state: CopilotState, deps: StageDeps) -> CopilotState:
        for name, stage in _PIPELINE:
            state = await stage(state, deps)
            if state.halted and name != "validate":
                # Still run validation on a halted state: safety findings are worth
                # reporting even when the answer is already suppressed, and the clinician
                # is better served knowing *why* than knowing only that nothing came back.
                state = await stage_validate(state, deps)
                break
        return state

    def _finalise(
        self, state: CopilotState, *, mode_override: ResponseMode | None = None
    ) -> CopilotResult:
        """Turn a finished state into an answer and its explanation."""
        dropped = tuple(
            note
            for record in state.trace
            if record.stage == "reflect"
            for note in record.details.get("rejected", [])
        )

        if mode_override is not None:
            mode = mode_override
        elif state.halted:
            mode = _HALT_MODES.get(state.halted)
            if mode is None:
                raise RuntimeError(f"Unmapped halt reason '{state.halted}'; add it to _HALT_MODES")
        else:
            mode = ResponseMode.ANSWER

        text = state.draft_text
        uncertainty = None
        if mode is ResponseMode.UNCERTAIN:
            uncertainty = _uncertainty_text(state)
            text = text or uncertainty
        elif mode is ResponseMode.BLOCKED:
            blocking = [
                f for f in state.safety_findings if str(getattr(f, "severity", "")) == "block"
            ]
            text = text or (
                blocking[0].message
                if blocking
                else "This question cannot be answered safely from the available records."
            )
        elif mode is ResponseMode.NEEDS_APPROVAL and state.pending_approval:
            text = text or state.pending_approval.reason

        explanation = build_explanation(
            evidence=state.evidence,
            claims=state.claims,
            confidence=state.confidence,
            trace=state.trace,
            dropped=dropped,
            uncertainty=uncertainty or "",
        )

        answer = Answer(
            mode=mode,
            text=text,
            claims=state.claims,
            evidence=state.evidence,
            confidence=state.confidence,
            safety_findings=state.safety_findings,
            trace=state.trace,
            usage=state.usage,
            prompt_versions=state.prompt_versions,
            pending_approval=state.pending_approval,
            uncertainty_reason=uncertainty,
        )
        return CopilotResult(answer=answer, explanation=explanation, state=state)

    def _remember_turn(self, question: CopilotQuestion, result: CopilotResult) -> None:
        """Record the turn so the next one can refer back to it.

        A clarification request is recorded too: "which patient?" followed by "the one with
        the hyperkalemia" only works if the assistant remembers that it asked.
        """
        import datetime as dt

        memory = self._deps.memory.get(tenant_id=question.tenant_id, session_id=question.session_id)
        self._deps.memory.append_turn(
            tenant_id=question.tenant_id,
            session_id=question.session_id,
            turn=Turn(
                turn_id=len(memory.working) + len(memory.episodic) + 1,
                question=question.text,
                answer=result.answer.text,
                asked_at=dt.datetime.now(dt.UTC),
                patient_id=question.patient_id,
                confidence=result.answer.confidence.score,
                evidence_count=len(result.answer.cited_evidence()),
            ),
        )


def _uncertainty_text(state: CopilotState) -> str:
    """Explain what is missing, in terms a clinician can act on."""
    if state.halted == "no_model":
        return (
            "No language model is available to compose an answer. The evidence found for "
            "this question is listed below."
        )
    if state.halted == "unverified_text":
        return (
            "A draft answer was produced but it asserted something the cited evidence does "
            "not support, so it was withheld."
        )
    if state.halted == "invalid_plan":
        return "The assistant could not construct a valid plan for this question."
    if state.halted == "truncated":
        return (
            "The answer exceeded the output limit and was withheld rather than returned "
            "incomplete. The evidence found is listed below."
        )
    if not state.claims:
        return "No evidence in the available records supports an answer to this question."

    weakest = state.confidence.weakest().replace("_", " ")
    return (
        f"Confidence is {state.confidence.score:.2f}, below the threshold for answering. "
        f"The weakest component is {weakest}."
    )
