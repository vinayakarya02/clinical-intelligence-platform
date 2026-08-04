"""Domain value objects for the clinical copilot.

Everything the pipeline passes between stages lives here, and nothing here imports anything
else from this package. That is the base of the dependency order in
docs/design/adr-0008-copilot-module-boundaries.md, and a test enforces it.

Two types carry the design.

:class:`Evidence` is the *only* thing a claim may cite. A retrieved chunk, a graph edge, and
a tool result all become one, so the reasoning and verification stages do not need to know
where a fact came from — while ``kind`` and ``source_ref`` preserve exactly that for the
explanation and the audit trail.

:class:`Claim` is the only thing an answer may assert. A sentence that is not a claim, or a
claim whose supporting evidence does not check out, never reaches the clinician. That is the
mechanism behind "never produce unexplained conclusions": it is structural, not a prompt
instruction.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

__all__ = [
    "Answer",
    "Claim",
    "ClaimSupport",
    "ConfidenceBreakdown",
    "CopilotQuestion",
    "CopilotState",
    "Evidence",
    "EvidenceKind",
    "PendingApproval",
    "ResponseMode",
    "StageRecord",
    "TokenUsage",
]


class EvidenceKind(StrEnum):
    """Where a piece of evidence came from.

    Kept explicit rather than inferred: the explanation layer renders a graph inference and a
    quoted passage differently, and a tool-computed value differently again. Collapsing them
    would let a derived number be presented as something a clinician wrote.
    """

    DOCUMENT_CHUNK = "document_chunk"
    GRAPH_RELATIONSHIP = "graph_relationship"
    TOOL_RESULT = "tool_result"
    STRUCTURED_FACT = "structured_fact"

    @property
    def is_quotable(self) -> bool:
        """Whether this evidence may be presented as source text.

        Only a document chunk is something a human actually wrote. A graph edge is an
        inference and a tool result is a computation; quoting either would misattribute it.
        """
        return self is EvidenceKind.DOCUMENT_CHUNK


@dataclass(frozen=True, slots=True)
class Evidence:
    """One citable unit of support, whatever produced it."""

    id: str
    kind: EvidenceKind
    content: str
    tenant_id: uuid.UUID

    source_ref: str | None = None
    """Back-pointer to the origin — a chunk id, an edge key, a tool name. What an auditor
    follows to re-derive this evidence from the underlying store."""

    document_id: uuid.UUID | None = None
    patient_id: uuid.UUID | None = None
    section: str | None = None
    document_type: str | None = None
    effective_date: dt.date | None = None
    retrieval_score: float | None = None
    confidence: float = 1.0
    """How much the *producer* trusts this. A graph edge carries its extraction confidence; a
    retrieved chunk is 1.0 because the text is simply what the document says."""

    provenance: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("Evidence.id must not be empty")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("Evidence.confidence must be in [0, 1]")

    @property
    def age_days(self) -> int | None:
        """Days since the evidence was clinically effective, if dated."""
        if self.effective_date is None:
            return None
        return (dt.date.today() - self.effective_date).days

    def cite_label(self) -> str:
        """Short human-readable attribution for the citation list."""
        parts = [self.kind.value.replace("_", " ")]
        if self.section:
            parts.append(self.section)
        if self.document_type:
            parts.append(f"({self.document_type})")
        if self.effective_date:
            parts.append(self.effective_date.isoformat())
        return " · ".join(parts)


class ClaimSupport(StrEnum):
    """How well the cited evidence backs a claim.

    ``DIRECT`` means the evidence states it; ``DERIVED`` means it follows from combining
    several pieces; ``WEAK`` means the evidence is related but does not establish it. The
    distinction drives both confidence and how the claim is worded to the clinician.
    """

    DIRECT = "direct"
    DERIVED = "derived"
    WEAK = "weak"

    @property
    def weight(self) -> float:
        return {"direct": 1.0, "derived": 0.7, "weak": 0.35}[self.value]


@dataclass(frozen=True, slots=True)
class Claim:
    """One assertion, and the evidence that is supposed to support it."""

    id: str
    statement: str
    evidence_ids: tuple[str, ...]
    support: ClaimSupport = ClaimSupport.DIRECT
    numeric_values: tuple[str, ...] = ()
    """Numbers appearing in the statement, extracted at construction so the verifier can check
    each one against the cited evidence without re-parsing prose."""
    verified: bool | None = None
    """``None`` until reflection runs. A claim reaching output with ``None`` is a bug, and the
    validator treats it as one."""
    verification_notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.statement.strip():
            raise ValueError("Claim.statement must not be empty")
        if not self.evidence_ids:
            # The structural guarantee behind "never produce unexplained conclusions": a
            # claim with no evidence cannot be constructed, so it cannot be rendered.
            raise ValueError(f"Claim '{self.id}' has no supporting evidence")

    def with_verification(self, *, verified: bool, notes: tuple[str, ...] = ()) -> Claim:
        return replace(self, verified=verified, verification_notes=notes)


@dataclass(frozen=True, slots=True)
class ConfidenceBreakdown:
    """Named components of the confidence score, and the score itself.

    Decomposed because a single opaque number invites reliance it has not earned. A clinician
    who can see that confidence is low *because coverage is low* knows to ask a narrower
    question; one who sees only "0.41" learns nothing actionable.
    """

    evidence_strength: float = 0.0
    agreement: float = 0.0
    coverage: float = 0.0
    recency: float = 0.0
    source_quality: float = 0.0
    verification: float = 0.0

    def as_dict(self) -> dict[str, float]:
        return {
            "evidence_strength": self.evidence_strength,
            "agreement": self.agreement,
            "coverage": self.coverage,
            "recency": self.recency,
            "source_quality": self.source_quality,
            "verification": self.verification,
        }

    @property
    def score(self) -> float:
        """Weighted overall confidence in [0, 1].

        Verification is weighted highest: a claim that failed its evidence check is the
        strongest available signal that an answer should not be trusted, stronger than any
        amount of corroborating retrieval.
        """
        weights = {
            "evidence_strength": 0.20,
            "agreement": 0.15,
            "coverage": 0.20,
            "recency": 0.10,
            "source_quality": 0.10,
            "verification": 0.25,
        }
        values = self.as_dict()
        return round(sum(values[name] * weight for name, weight in weights.items()), 4)

    def weakest(self) -> str:
        """The component dragging the score down, for the uncertainty explanation."""
        return min(self.as_dict().items(), key=lambda item: item[1])[0]


class ResponseMode(StrEnum):
    """What kind of response the pipeline produced.

    ``UNCERTAIN`` and ``BLOCKED`` are first-class outcomes rather than errors: declining to
    answer is frequently the clinically correct response, and modelling it as a failure would
    push callers toward retrying until they got prose.
    """

    ANSWER = "answer"
    UNCERTAIN = "uncertain"
    BLOCKED = "blocked"
    NEEDS_APPROVAL = "needs_approval"
    CLARIFICATION = "clarification"


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """Token accounting for one or more model calls."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    calls: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def __add__(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            calls=self.calls + other.calls,
        )

    def cost_usd(self, *, prompt_per_1k: float, completion_per_1k: float) -> float:
        """Estimated cost. Rates are per-provider and belong to configuration, not here."""
        return round(
            self.prompt_tokens / 1000 * prompt_per_1k
            + self.completion_tokens / 1000 * completion_per_1k,
            6,
        )


@dataclass(frozen=True, slots=True)
class StageRecord:
    """What one pipeline stage did.

    The unit of the reasoning trace. Recorded for every stage on every request, not only on
    failure — "why did it answer that" is asked about answers that looked fine.
    """

    stage: str
    duration_ms: float
    summary: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "duration_ms": round(self.duration_ms, 3),
            "summary": self.summary,
            "details": self.details,
        }


@dataclass(frozen=True, slots=True)
class CopilotQuestion:
    """An inbound question, scoped."""

    text: str
    tenant_id: uuid.UUID
    session_id: str
    patient_id: uuid.UUID | None = None
    request_id: str | None = None
    mode: str = "clinician"
    max_evidence: int = 12

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("Question text must not be empty")
        if not self.session_id.strip():
            raise ValueError("session_id must not be empty")


@dataclass(frozen=True, slots=True)
class PendingApproval:
    """A run suspended awaiting a human decision."""

    step_id: str
    tool_name: str
    reason: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Answer:
    """The validated result of one turn.

    Frozen and self-contained: every renderer is a pure projection of this object, so two
    output surfaces cannot disagree about what was said.
    """

    mode: ResponseMode
    text: str
    claims: tuple[Claim, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    confidence: ConfidenceBreakdown = field(default_factory=ConfidenceBreakdown)
    safety_findings: tuple[Any, ...] = ()
    trace: tuple[StageRecord, ...] = ()
    usage: TokenUsage = field(default_factory=TokenUsage)
    prompt_versions: dict[str, str] = field(default_factory=dict)
    pending_approval: PendingApproval | None = None
    uncertainty_reason: str | None = None

    @property
    def is_answered(self) -> bool:
        return self.mode is ResponseMode.ANSWER

    def evidence_by_id(self) -> dict[str, Evidence]:
        return {item.id: item for item in self.evidence}

    def cited_evidence(self) -> tuple[Evidence, ...]:
        """Only the evidence a surviving claim actually cites.

        The evidence *set* is what retrieval and tools produced; the *cited* subset is what
        the answer rests on. Showing the former as though it were the latter would overstate
        support, which is the same defect as a padded bibliography.
        """
        cited = {eid for claim in self.claims for eid in claim.evidence_ids}
        return tuple(item for item in self.evidence if item.id in cited)


@dataclass(frozen=True, slots=True)
class CopilotState:
    """The value threaded through every pipeline stage.

    Immutable, and every stage returns a new one via :meth:`advanced`. That is what makes each
    stage a pure function testable with a hand-built state, and what makes the run
    checkpointable for human-in-the-loop suspension: the state at any boundary is the whole
    of what a resume needs (docs/design/adr-0012-language-model-seam.md).
    """

    question: CopilotQuestion
    resolved_text: str = ""
    """The question after memory resolves references ("his creatinine" → a patient id). Kept
    separate from the original so the trace shows what was rewritten."""

    plan: Any = None
    evidence: tuple[Evidence, ...] = ()
    tool_data: dict[str, Any] = field(default_factory=dict)
    """Structured payloads from tool calls, keyed by capability. Carried on the state rather
    than returned alongside it so every stage keeps the same ``(state) -> state`` shape —
    the property the whole pipeline's testability rests on."""
    claims: tuple[Claim, ...] = ()
    draft_text: str = ""
    confidence: ConfidenceBreakdown = field(default_factory=ConfidenceBreakdown)
    safety_findings: tuple[Any, ...] = ()
    trace: tuple[StageRecord, ...] = ()
    usage: TokenUsage = field(default_factory=TokenUsage)
    prompt_versions: dict[str, str] = field(default_factory=dict)
    pending_approval: PendingApproval | None = None
    halted: str | None = None
    """Set by a stage that ends the run early — a safety block or an approval suspension.
    Later stages check it rather than each one re-deriving whether it should run."""

    def advanced(self, record: StageRecord, **changes: Any) -> CopilotState:
        """Return the next state with ``record`` appended to the trace."""
        return replace(self, trace=(*self.trace, record), **changes)

    def evidence_by_id(self) -> dict[str, Evidence]:
        return {item.id: item for item in self.evidence}
