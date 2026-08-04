"""Clinical Copilot — the intelligence layer over retrieval.

Phase 3. Turns a clinician's question into a cited, verified, explained answer, or
into an honest statement of why one cannot be given.
"""

from cip_copilot.domain import (
    Answer,
    Claim,
    ConfidenceBreakdown,
    CopilotQuestion,
    CopilotState,
    Evidence,
    EvidenceKind,
    ResponseMode,
)
from cip_copilot.orchestrator import ClinicalCopilot, CopilotResult

__all__ = [
    "Answer",
    "Claim",
    "ClinicalCopilot",
    "ConfidenceBreakdown",
    "CopilotQuestion",
    "CopilotResult",
    "CopilotState",
    "Evidence",
    "EvidenceKind",
    "ResponseMode",
]
