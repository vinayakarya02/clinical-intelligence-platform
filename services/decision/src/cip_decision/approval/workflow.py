"""Human approval.

Every recommendation enters a lifecycle and **nothing reaches ``ACCEPTED`` without an
identified human** (docs/design/adr-0024-human-approval-gate.md). There is no auto-accept, no
confidence threshold that skips review, and no configuration flag that disables the gate.

The gate is not primarily about the recommendation; it is about accountability. A clinical
action needs an accountable clinician, and a system that can act without one has moved the
accountability to the vendor while leaving the consequence with the patient.

A rejection **requires a reason**, because that is the most valuable datum the system
collects: the only direct measurement of whether the knowledge base is right.
"""

from __future__ import annotations

import datetime as dt
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from cip_core.errors import CipError
from cip_core.logging import get_logger
from cip_decision.domain import Recommendation, ReviewState

__all__ = ["ApprovalError", "ApprovalRecord", "ApprovalWorkflow", "ReviewDecision"]

_log = get_logger(__name__)

#: Legal transitions. A table rather than scattered conditionals, so the reachable states are
#: reviewable in one place — and so nothing can reach ACCEPTED by an unconsidered path.
_TRANSITIONS: dict[ReviewState, frozenset[ReviewState]] = {
    ReviewState.PROPOSED: frozenset(
        {ReviewState.UNDER_REVIEW, ReviewState.SUPPRESSED, ReviewState.EXPIRED}
    ),
    ReviewState.UNDER_REVIEW: frozenset(
        {ReviewState.ACCEPTED, ReviewState.REJECTED, ReviewState.EXPIRED}
    ),
    ReviewState.ACCEPTED: frozenset(),
    ReviewState.REJECTED: frozenset(),
    ReviewState.EXPIRED: frozenset(),
    ReviewState.SUPPRESSED: frozenset(),
}


class ApprovalError(CipError):
    """An invalid lifecycle operation."""

    status = 409
    problem_type = "approval-invalid"
    title = "Approval operation refused"


@dataclass(frozen=True, slots=True)
class ReviewDecision:
    """One clinician's decision."""

    reviewer_id: str
    state: ReviewState
    reason: str = ""
    at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))
    note: str = ""

    def __post_init__(self) -> None:
        if not self.reviewer_id.strip():
            raise ApprovalError("A review decision requires an identified reviewer")
        if self.state is ReviewState.REJECTED and not self.reason.strip():
            raise ApprovalError(
                "A rejection requires a reason. It is the only direct measurement of whether "
                "the knowledge base is right, and it feeds alert suppression."
            )


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    """A recommendation's full review history."""

    recommendation: Recommendation
    state: ReviewState
    history: tuple[ReviewDecision, ...] = ()
    expires_at: dt.datetime | None = None

    @property
    def is_open(self) -> bool:
        return not self.state.is_terminal

    @property
    def decided_by(self) -> str:
        return self.history[-1].reviewer_id if self.history else ""

    def to_json(self) -> dict[str, Any]:
        return {
            "recommendation_id": self.recommendation.id,
            "state": str(self.state),
            "severity": str(self.recommendation.severity),
            "decided_by": self.decided_by,
            "history": [
                {
                    "reviewer": d.reviewer_id,
                    "state": str(d.state),
                    "reason": d.reason,
                    "at": d.at.isoformat(),
                }
                for d in self.history
            ],
        }


class ApprovalWorkflow:
    """Tracks recommendations through review.

    Emits an audit event on every transition. The audit sink is injected — Phase 1's
    hash-chained log in production — so an approval cannot be backdated or removed without
    detection.
    """

    def __init__(
        self,
        *,
        default_ttl_hours: int = 72,
        audit_sink: Any = None,
        max_closed_records: int = 5000,
    ) -> None:
        self._records: OrderedDict[str, ApprovalRecord] = OrderedDict()
        self._ttl = dt.timedelta(hours=default_ttl_hours)
        self._audit = audit_sink
        self._max_closed = max_closed_records
        """Closed records are evicted oldest-first past this bound. **Open records are never
        evicted**, whatever the bound: dropping a pending review loses a clinical decision
        somebody is waiting on, and an unbounded map is the lesser problem next to that.

        Without any bound this map grows one entry per recommendation forever, retaining
        clinical content tied to patient ids for the life of the process — a memory leak and
        a PHI-retention issue at once. Production persists this to a database; the bound is
        what makes the in-process implementation safe to run."""

    def submit(
        self, recommendation: Recommendation, *, now: dt.datetime | None = None
    ) -> ApprovalRecord:
        """Enter a recommendation into review."""
        moment = now or dt.datetime.now(dt.UTC)
        if recommendation.id in self._records:
            return self._records[recommendation.id]

        record = ApprovalRecord(
            recommendation=recommendation,
            state=ReviewState.PROPOSED,
            expires_at=moment + self._ttl,
        )
        self._records[recommendation.id] = record
        self._evict_closed()
        self._emit("submitted", record, moment)
        return record

    def claim(self, recommendation_id: str, *, reviewer_id: str) -> ApprovalRecord:
        """A named clinician takes the recommendation for review."""
        return self._transition(
            recommendation_id,
            ReviewDecision(reviewer_id=reviewer_id, state=ReviewState.UNDER_REVIEW),
        )

    def accept(self, recommendation_id: str, *, reviewer_id: str, note: str = "") -> ApprovalRecord:
        """Accept — only ever from ``UNDER_REVIEW``, and only by a named human.

        Requiring the claim step first is deliberate: it makes "somebody looked at this"
        a recorded fact rather than an inference from the acceptance.
        """
        return self._transition(
            recommendation_id,
            ReviewDecision(reviewer_id=reviewer_id, state=ReviewState.ACCEPTED, note=note),
        )

    def reject(self, recommendation_id: str, *, reviewer_id: str, reason: str) -> ApprovalRecord:
        """Reject with a reason. The reason is mandatory."""
        return self._transition(
            recommendation_id,
            ReviewDecision(reviewer_id=reviewer_id, state=ReviewState.REJECTED, reason=reason),
        )

    def expire_stale(self, *, now: dt.datetime | None = None) -> tuple[ApprovalRecord, ...]:
        """Close recommendations nobody reviewed in time.

        A recommendation about a lab value from three days ago is not a pending decision, it
        is stale. Expiring it explicitly beats letting a queue accumulate items nobody will
        action, which is how a review queue stops being read at all.
        """
        moment = now or dt.datetime.now(dt.UTC)
        expired: list[ApprovalRecord] = []
        for record in list(self._records.values()):
            if not record.is_open or record.expires_at is None:
                continue
            if moment >= record.expires_at:
                updated = ApprovalRecord(
                    recommendation=record.recommendation.with_state(ReviewState.EXPIRED),
                    state=ReviewState.EXPIRED,
                    history=(
                        *record.history,
                        ReviewDecision(
                            reviewer_id="system",
                            state=ReviewState.EXPIRED,
                            reason="not reviewed before expiry",
                            at=moment,
                        ),
                    ),
                    expires_at=record.expires_at,
                )
                self._records[record.recommendation.id] = updated
                self._emit("expired", updated, moment)
                expired.append(updated)
        return tuple(expired)

    def get(self, recommendation_id: str) -> ApprovalRecord | None:
        return self._records.get(recommendation_id)

    def open_records(self) -> tuple[ApprovalRecord, ...]:
        return tuple(
            sorted(
                (r for r in self._records.values() if r.is_open),
                key=lambda r: (-r.recommendation.severity.rank, r.recommendation.id),
            )
        )

    def rejection_reasons(self) -> dict[str, int]:
        """Why clinicians are rejecting, counted.

        The feedback loop on knowledge quality: a rule rejected repeatedly for the same reason
        is a rule that is wrong, and this is where that becomes visible.
        """
        counts: dict[str, int] = {}
        for record in self._records.values():
            for decision in record.history:
                if decision.state is ReviewState.REJECTED:
                    counts[decision.reason] = counts.get(decision.reason, 0) + 1
        return counts

    def _transition(self, recommendation_id: str, decision: ReviewDecision) -> ApprovalRecord:
        record = self._records.get(recommendation_id)
        if record is None:
            raise ApprovalError(f"No recommendation '{recommendation_id}' is under review")

        allowed = _TRANSITIONS[record.state]
        if decision.state not in allowed:
            raise ApprovalError(
                f"Cannot move '{recommendation_id}' from {record.state} to {decision.state}. "
                f"Allowed: {sorted(s.value for s in allowed) or 'none — this state is terminal'}"
            )

        updated = ApprovalRecord(
            recommendation=record.recommendation.with_state(decision.state),
            state=decision.state,
            history=(*record.history, decision),
            expires_at=record.expires_at,
        )
        self._records[recommendation_id] = updated
        self._evict_closed()
        self._emit(decision.state.value, updated, decision.at)
        return updated

    def _evict_closed(self) -> None:
        """Drop the oldest terminal records past the bound.

        Open records are skipped entirely. A queue that evicted a pending review to save
        memory would lose a decision a clinician is waiting to make, which is a categorically
        worse failure than the memory it saves.
        """
        closed = [rid for rid, record in self._records.items() if not record.is_open]
        excess = len(closed) - self._max_closed
        for rid in closed[:excess] if excess > 0 else []:
            del self._records[rid]

    def _emit(self, action: str, record: ApprovalRecord, at: dt.datetime) -> None:
        _log.info(
            "approval.transition",
            action=action,
            recommendation=record.recommendation.id,
            state=str(record.state),
            reviewer=record.decided_by,
        )
        if self._audit is not None:
            self._audit(
                {
                    "action": action,
                    "recommendation_id": record.recommendation.id,
                    "state": str(record.state),
                    "reviewer": record.decided_by,
                    "at": at.isoformat(),
                }
            )
