"""Clinical Decision Intelligence.

Phase 5. Turns the platform from one that answers questions into one that proposes
safe, explainable, evidence-based actions — deterministically, from versioned cited
knowledge, with nothing reaching a patient record without human review.

**The clinical knowledge corpus shipped here has not been clinically reviewed and must not be
used in the care of real patients.** See docs/safety/clinical-safety-case.md.
"""

from cip_decision.domain import (
    Citation,
    ClinicalFact,
    EvidenceQuality,
    FactKind,
    PatientContext,
    Recommendation,
    RecommendationKind,
    ReviewState,
    Severity,
)
from cip_decision.engine import DecisionEngine, DecisionResult

__all__ = [
    "Citation",
    "ClinicalFact",
    "DecisionEngine",
    "DecisionResult",
    "EvidenceQuality",
    "FactKind",
    "PatientContext",
    "Recommendation",
    "RecommendationKind",
    "ReviewState",
    "Severity",
]
