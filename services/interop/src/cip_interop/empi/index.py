"""The Enterprise Master Patient Index.

Resolves source records to people, queues what it cannot decide, and keeps merges reversible.

The whole design follows from an asymmetry. A **false match** merges two people, producing one
chart containing someone else's allergies and diagnoses with nothing to indicate it is two
people — that is the error that kills. A **false non-match** splits one person, so a clinician
sees a thin record rather than a confidently wrong one. Both are bad; only one is quiet.

So: ambiguity goes to a human, merges are links rather than rewrites, and a merge that
contradicts a nationally-unique identifier is refused outright regardless of what the score
says (docs/design/adr-0027-empi-review-not-automerge.md).

**Identity resolution is not authorisation.** Knowing that a record at one organisation and a
record at another describe the same person grants nobody the right to read either
(docs/design/adr-0030-cross-organisation-sharing.md).
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from cip_core.logging import get_logger
from cip_interop.domain import InteropError, PersonRecord
from cip_interop.empi.matching import (
    DEFAULT_BLOCKING,
    BlockingIndex,
    BlockingStrategy,
    MatchingModel,
    MatchScore,
    MatchZone,
    default_model,
)

__all__ = [
    "EmpiError",
    "EmpiIndex",
    "LinkOrigin",
    "MergeRecord",
    "PersonLink",
    "Resolution",
    "ReviewDecision",
    "ReviewTask",
]

_log = get_logger(__name__)


class EmpiError(InteropError):
    """A requested identity operation is not safe or not possible."""


class LinkOrigin(StrEnum):
    """How a record came to be attached to a person."""

    NEW_PERSON = "new_person"
    AUTOMATIC = "automatic"
    """Score above the upper threshold."""
    DETERMINISTIC = "deterministic"
    """A shared nationally-unique identifier."""
    HUMAN = "human"
    """A steward decided."""
    HL7_MERGE = "hl7_merge"
    """An ADT merge message instructed it."""

    @property
    def is_reviewable(self) -> bool:
        """Whether this link was made without a human and could therefore be wrong quietly."""
        return self in (LinkOrigin.AUTOMATIC, LinkOrigin.DETERMINISTIC)


@dataclass(frozen=True, slots=True)
class PersonLink:
    """One record's attachment to a person.

    A link, never a rewrite. The source record keeps its own identifiers and stays individually
    addressable, which is what makes an unmerge a restoration rather than a reconstruction.
    """

    link_id: str
    person_id: str
    record_id: str
    origin: LinkOrigin
    score: float = 0.0
    established_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))
    established_by: str = ""
    reason: str = ""
    broken_at: dt.datetime | None = None
    broken_by: str = ""

    @property
    def active(self) -> bool:
        return self.broken_at is None

    def render(self) -> str:
        state = "active" if self.active else f"broken {self.broken_at:%Y-%m-%d}"
        return f"{self.record_id} -> {self.person_id} ({self.origin.value}, {state})"


@dataclass(frozen=True, slots=True)
class MergeRecord:
    """One merge of two people, and whether it has been reversed."""

    merge_id: str
    surviving_person_id: str
    absorbed_person_id: str
    moved_record_ids: tuple[str, ...]
    performed_by: str
    reason: str
    at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))
    reversed_at: dt.datetime | None = None
    reversed_by: str = ""
    reversal_reason: str = ""

    @property
    def reversed_merge(self) -> bool:
        return self.reversed_at is not None

    def to_json(self) -> dict[str, Any]:
        return {
            "merge_id": self.merge_id,
            "surviving": self.surviving_person_id,
            "absorbed": self.absorbed_person_id,
            "records": list(self.moved_record_ids),
            "by": self.performed_by,
            "reason": self.reason,
            "at": self.at.isoformat(),
            "reversed": self.reversed_merge,
        }


class ReviewDecision(StrEnum):
    """What a steward concluded about a queued pair."""

    SAME_PERSON = "same_person"
    DIFFERENT_PEOPLE = "different_people"
    INSUFFICIENT_INFORMATION = "insufficient_information"
    """Neither. The task stays open and the record stays on its own person — which is the safe
    resting state, because an undecided pair left separate is a split, not a mixed chart."""


@dataclass(frozen=True, slots=True)
class ReviewTask:
    """A pair the matcher would not decide."""

    task_id: str
    record_id: str
    candidate_person_id: str
    score: MatchScore
    created_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))
    decision: ReviewDecision | None = None
    decided_by: str = ""
    decided_at: dt.datetime | None = None
    note: str = ""

    @property
    def open(self) -> bool:
        return self.decision is None or self.decision is ReviewDecision.INSUFFICIENT_INFORMATION

    def age_hours(self, now: dt.datetime | None = None) -> float:
        return ((now or dt.datetime.now(dt.UTC)) - self.created_at).total_seconds() / 3600


@dataclass(frozen=True, slots=True)
class Resolution:
    """What happened when a record was ingested."""

    record_id: str
    person_id: str
    zone: MatchZone
    origin: LinkOrigin
    score: MatchScore | None = None
    review_task_id: str = ""
    candidates_considered: int = 0

    @property
    def needs_review(self) -> bool:
        return bool(self.review_task_id)

    def to_json(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "person_id": self.person_id,
            "zone": str(self.zone),
            "origin": str(self.origin),
            "score": self.score.total if self.score else None,
            "review_task": self.review_task_id,
            "candidates": self.candidates_considered,
        }


class EmpiIndex:
    """Identity resolution across organisations."""

    def __init__(
        self,
        *,
        model: MatchingModel | None = None,
        blocking: tuple[BlockingStrategy, ...] = DEFAULT_BLOCKING,
        max_candidates: int = 200,
    ) -> None:
        self._model = model or default_model()
        self._blocking = BlockingIndex(strategies=blocking)
        self._records: dict[str, PersonRecord] = {}
        self._links: dict[str, PersonLink] = {}
        self._person_of_record: dict[str, str] = {}
        self._records_of_person: dict[str, set[str]] = {}
        self._merges: OrderedDict[str, MergeRecord] = OrderedDict()
        self._resource_aliases: dict[tuple[str, str, str], str] = {}
        """(organisation, resource type, resource id) -> person id.

        The join between organisation-local FHIR resource ids and the canonical person. Without
        it, anything keyed on the person — consent above all — is looked up under an
        organisation-local id, so a patient with records at two organisations would need two
        consents and revoking one would not affect the other."""
        self._reviews: OrderedDict[str, ReviewTask] = OrderedDict()
        self._max_candidates = max_candidates
        """Scoring is capped per ingest. An unbounded candidate set turns one pathological
        blocking bucket — everyone recorded as born on 1 January — into a quadratic stall on
        the ingest path, which is a denial of service reachable by ordinary bad data."""

    @property
    def model(self) -> MatchingModel:
        return self._model

    def record_count(self) -> int:
        return len(self._records)

    def person_count(self) -> int:
        return sum(1 for records in self._records_of_person.values() if records)

    def ingest(self, record: PersonRecord, *, at: dt.datetime | None = None) -> Resolution:
        """Add a source record and resolve it to a person."""
        if record.record_id in self._records:
            raise EmpiError(
                f"record {record.record_id!r} is already indexed; re-ingesting would create a "
                "second copy that scores as a perfect match against the first"
            )

        candidates = self._blocking.candidates(record)
        best_person = ""
        best_score: MatchScore | None = None
        considered = 0

        for candidate_id in sorted(candidates)[: self._max_candidates]:
            other = self._records.get(candidate_id)
            if other is None:
                continue
            considered += 1
            score = self._model.compare(record, other)
            if best_score is None or score.total > best_score.total:
                best_score, best_person = score, self._person_of_record.get(candidate_id, "")

        self._records[record.record_id] = record
        self._blocking.add(record)

        if best_score is None or not best_person:
            person_id = self._new_person()
            self._attach(record.record_id, person_id, LinkOrigin.NEW_PERSON, 0.0, at=at)
            return Resolution(
                record_id=record.record_id,
                person_id=person_id,
                zone=MatchZone.NON_MATCH,
                origin=LinkOrigin.NEW_PERSON,
                candidates_considered=considered,
            )

        if best_score.zone is MatchZone.MATCH:
            conflict = self._identifier_conflict(record, best_person)
            if conflict:
                # A high score that a nationally-unique identifier contradicts is exactly the
                # case worth stopping on: twins, or a family sharing a phone and address. The
                # score is not overruled silently — the pair goes to a human.
                task = self._queue_review(record, best_person, best_score, at=at)
                person_id = self._new_person()
                self._attach(record.record_id, person_id, LinkOrigin.NEW_PERSON, 0.0, at=at)
                _log.warning(
                    "empi.identifier_conflict",
                    record=record.record_id,
                    candidate_person=best_person,
                    conflict=conflict,
                )
                return Resolution(
                    record_id=record.record_id,
                    person_id=person_id,
                    zone=MatchZone.REVIEW,
                    origin=LinkOrigin.NEW_PERSON,
                    score=best_score,
                    review_task_id=task.task_id,
                    candidates_considered=considered,
                )

            origin = (
                LinkOrigin.DETERMINISTIC
                if best_score.deterministic_override
                else LinkOrigin.AUTOMATIC
            )
            self._attach(record.record_id, best_person, origin, best_score.total, at=at)
            return Resolution(
                record_id=record.record_id,
                person_id=best_person,
                zone=MatchZone.MATCH,
                origin=origin,
                score=best_score,
                candidates_considered=considered,
            )

        if best_score.zone is MatchZone.REVIEW:
            task = self._queue_review(record, best_person, best_score, at=at)
            person_id = self._new_person()
            self._attach(record.record_id, person_id, LinkOrigin.NEW_PERSON, 0.0, at=at)
            return Resolution(
                record_id=record.record_id,
                person_id=person_id,
                zone=MatchZone.REVIEW,
                origin=LinkOrigin.NEW_PERSON,
                score=best_score,
                review_task_id=task.task_id,
                candidates_considered=considered,
            )

        person_id = self._new_person()
        self._attach(record.record_id, person_id, LinkOrigin.NEW_PERSON, 0.0, at=at)
        return Resolution(
            record_id=record.record_id,
            person_id=person_id,
            zone=MatchZone.NON_MATCH,
            origin=LinkOrigin.NEW_PERSON,
            score=best_score,
            candidates_considered=considered,
        )

    def _identifier_conflict(self, record: PersonRecord, person_id: str) -> str:
        """A nationally-unique identifier held with a *different value* by this person.

        Two records both carrying a national identifier, with different values, are two people
        — whatever their names and birth dates say.
        """
        mine = {
            i.system.strip().lower(): i.value.strip()
            for i in record.matching_identifiers()
            if i.is_nationally_unique
        }
        if not mine:
            return ""
        for other_id in self._records_of_person.get(person_id, set()):
            other = self._records.get(other_id)
            if other is None:
                continue
            for identifier in other.matching_identifiers():
                if not identifier.is_nationally_unique:
                    continue
                system = identifier.system.strip().lower()
                if system in mine and mine[system] != identifier.value.strip():
                    return (
                        f"{identifier.system} differs: {mine[system]!r} vs "
                        f"{identifier.value.strip()!r}"
                    )
        return ""

    def _new_person(self) -> str:
        # Hyphen, not colon. A person id is used as a FHIR Patient resource id and as a stream
        # partition key, and FHIR ids permit only A-Z a-z 0-9 - and dot. A colon here produced
        # references that every FHIR consumer rejects, and the rejection surfaced three layers
        # away in imaging.
        person_id = f"person-{uuid.uuid4()}"
        self._records_of_person[person_id] = set()
        return person_id

    def _attach(
        self,
        record_id: str,
        person_id: str,
        origin: LinkOrigin,
        score: float,
        *,
        by: str = "",
        reason: str = "",
        at: dt.datetime | None = None,
    ) -> PersonLink:
        link = PersonLink(
            link_id=f"link:{uuid.uuid4()}",
            person_id=person_id,
            record_id=record_id,
            origin=origin,
            score=score,
            established_by=by,
            reason=reason,
            established_at=at or dt.datetime.now(dt.UTC),
        )
        self._links[link.link_id] = link
        self._person_of_record[record_id] = person_id
        self._records_of_person.setdefault(person_id, set()).add(record_id)
        return link

    def _queue_review(
        self,
        record: PersonRecord,
        candidate_person_id: str,
        score: MatchScore,
        *,
        at: dt.datetime | None = None,
    ) -> ReviewTask:
        task = ReviewTask(
            task_id=f"review:{uuid.uuid4()}",
            record_id=record.record_id,
            candidate_person_id=candidate_person_id,
            score=score,
            created_at=at or dt.datetime.now(dt.UTC),
        )
        self._reviews[task.task_id] = task
        return task

    def register_resource_alias(
        self, *, organization_id: str, resource_type: str, resource_id: str, person_id: str
    ) -> None:
        """Associate an organisation-local FHIR resource with a person."""
        self._resource_aliases[(organization_id, resource_type, resource_id)] = person_id

    def person_for_resource(
        self, organization_id: str, resource_type: str, resource_id: str
    ) -> str:
        """The person an organisation-local resource belongs to, or ``""`` if unknown.

        ``""`` rather than a fallback to ``resource_id``. Falling back would make consent
        evaluate against an organisation-local identifier that no consent is filed under, and
        the resulting "no consent on file" looks like a data-entry gap rather than a bug.
        """
        return self._resource_aliases.get((organization_id, resource_type, resource_id), "")

    def person_for(self, record_id: str) -> str:
        person_id = self._person_of_record.get(record_id)
        if person_id is None:
            raise EmpiError(f"record {record_id!r} is not indexed")
        return person_id

    def records_for(self, person_id: str) -> tuple[PersonRecord, ...]:
        return tuple(
            self._records[r]
            for r in sorted(self._records_of_person.get(person_id, set()))
            if r in self._records
        )

    def organizations_for(self, person_id: str) -> tuple[str, ...]:
        """Which organisations hold a record for this person.

        Knowing this is **not** permission to read any of them. It is what a cross-organisation
        query needs in order to ask the holding organisations, each of which decides for itself.
        """
        return tuple(sorted({r.organization_id for r in self.records_for(person_id)}))

    def merge(
        self,
        surviving_person_id: str,
        absorbed_person_id: str,
        *,
        performed_by: str,
        reason: str,
        origin: LinkOrigin = LinkOrigin.HUMAN,
        at: dt.datetime | None = None,
    ) -> MergeRecord:
        """Merge two people.

        Refused without a named actor and a reason: a merge nobody can be asked about is one
        nobody will correct. Also refused when the two hold different values for the same
        nationally-unique identifier, whatever instructed the merge — including an ADT merge
        message, because a sending system can be wrong about which two records are one person.
        """
        if surviving_person_id == absorbed_person_id:
            raise EmpiError("cannot merge a person into themselves")
        for person_id in (surviving_person_id, absorbed_person_id):
            if person_id not in self._records_of_person:
                raise EmpiError(f"unknown person {person_id!r}")
        if not performed_by.strip():
            raise EmpiError(
                "a merge requires a named actor; an anonymous merge cannot be reviewed or "
                "questioned afterwards"
            )
        if not reason.strip():
            raise EmpiError("a merge requires a reason")

        conflict = self._cross_person_identifier_conflict(surviving_person_id, absorbed_person_id)
        if conflict:
            raise EmpiError(
                f"refusing to merge {absorbed_person_id} into {surviving_person_id}: {conflict}. "
                "Two records carrying different values for the same nationally-unique "
                "identifier are two people, whatever else agrees."
            )

        moved = tuple(sorted(self._records_of_person.get(absorbed_person_id, set())))
        moment = at or dt.datetime.now(dt.UTC)
        for record_id in moved:
            self._break_links(record_id, by=performed_by, at=moment)
            self._attach(
                record_id,
                surviving_person_id,
                origin,
                0.0,
                by=performed_by,
                reason=reason,
                at=moment,
            )
        self._records_of_person[absorbed_person_id] = set()

        merge = MergeRecord(
            merge_id=f"merge:{uuid.uuid4()}",
            surviving_person_id=surviving_person_id,
            absorbed_person_id=absorbed_person_id,
            moved_record_ids=moved,
            performed_by=performed_by,
            reason=reason,
            at=moment,
        )
        self._merges[merge.merge_id] = merge
        _log.info(
            "empi.merged",
            surviving=surviving_person_id,
            absorbed=absorbed_person_id,
            records=len(moved),
            by=performed_by,
        )
        return merge

    def _cross_person_identifier_conflict(self, left: str, right: str) -> str:
        left_ids: dict[str, str] = {}
        for record in self.records_for(left):
            for identifier in record.matching_identifiers():
                if identifier.is_nationally_unique:
                    left_ids[identifier.system.strip().lower()] = identifier.value.strip()
        for record in self.records_for(right):
            for identifier in record.matching_identifiers():
                if not identifier.is_nationally_unique:
                    continue
                system = identifier.system.strip().lower()
                if system in left_ids and left_ids[system] != identifier.value.strip():
                    return (
                        f"{identifier.system} differs "
                        f"({left_ids[system]!r} vs {identifier.value.strip()!r})"
                    )
        return ""

    def _break_links(self, record_id: str, *, by: str, at: dt.datetime) -> None:
        for link_id, link in list(self._links.items()):
            if link.record_id == record_id and link.active:
                self._links[link_id] = PersonLink(
                    link_id=link.link_id,
                    person_id=link.person_id,
                    record_id=link.record_id,
                    origin=link.origin,
                    score=link.score,
                    established_at=link.established_at,
                    established_by=link.established_by,
                    reason=link.reason,
                    broken_at=at,
                    broken_by=by,
                )
                self._records_of_person.get(link.person_id, set()).discard(record_id)

    def unmerge(
        self,
        merge_id: str,
        *,
        performed_by: str,
        reason: str,
        at: dt.datetime | None = None,
    ) -> MergeRecord:
        """Reverse a merge.

        Possible because the merge never destroyed anything: the absorbed person id and the
        record ids are both still real, so this restores rather than reconstructs.
        """
        merge = self._merges.get(merge_id)
        if merge is None:
            raise EmpiError(f"unknown merge {merge_id!r}")
        if merge.reversed_merge:
            raise EmpiError(f"merge {merge_id!r} has already been reversed")
        if not performed_by.strip() or not reason.strip():
            raise EmpiError("an unmerge requires a named actor and a reason")

        moment = at or dt.datetime.now(dt.UTC)
        for record_id in merge.moved_record_ids:
            # Only records still on the surviving person are moved back. One that has since
            # been moved elsewhere by a later decision is left alone — undoing a merge must not
            # silently undo whatever happened after it.
            if self._person_of_record.get(record_id) != merge.surviving_person_id:
                continue
            self._break_links(record_id, by=performed_by, at=moment)
            self._attach(
                record_id,
                merge.absorbed_person_id,
                LinkOrigin.HUMAN,
                0.0,
                by=performed_by,
                reason=reason,
                at=moment,
            )

        reversed_merge = MergeRecord(
            merge_id=merge.merge_id,
            surviving_person_id=merge.surviving_person_id,
            absorbed_person_id=merge.absorbed_person_id,
            moved_record_ids=merge.moved_record_ids,
            performed_by=merge.performed_by,
            reason=merge.reason,
            at=merge.at,
            reversed_at=moment,
            reversed_by=performed_by,
            reversal_reason=reason,
        )
        self._merges[merge_id] = reversed_merge
        _log.info("empi.unmerged", merge=merge_id, by=performed_by)
        return reversed_merge

    def split(
        self,
        record_id: str,
        *,
        performed_by: str,
        reason: str,
        at: dt.datetime | None = None,
    ) -> str:
        """Detach one record onto a person of its own.

        The corrective for a false match discovered on a single record, rather than an
        unmerge of a whole batch. Returns the new person id.
        """
        if record_id not in self._records:
            raise EmpiError(f"record {record_id!r} is not indexed")
        if not performed_by.strip() or not reason.strip():
            raise EmpiError("a split requires a named actor and a reason")
        moment = at or dt.datetime.now(dt.UTC)
        self._break_links(record_id, by=performed_by, at=moment)
        person_id = self._new_person()
        self._attach(
            record_id,
            person_id,
            LinkOrigin.HUMAN,
            0.0,
            by=performed_by,
            reason=reason,
            at=moment,
        )
        _log.info("empi.split", record=record_id, person=person_id, by=performed_by)
        return person_id

    def open_reviews(self) -> tuple[ReviewTask, ...]:
        return tuple(t for t in self._reviews.values() if t.open)

    def review_queue_depth(self) -> int:
        """The number a monitor watches.

        The failure mode of a review queue is that nobody looks at it, and an unwatched queue
        turns "ambiguity goes to a human" into "ambiguity is discarded".
        """
        return len(self.open_reviews())

    def oldest_review_age_hours(self, now: dt.datetime | None = None) -> float:
        ages = [t.age_hours(now) for t in self.open_reviews()]
        return max(ages) if ages else 0.0

    def decide_review(
        self,
        task_id: str,
        decision: ReviewDecision,
        *,
        decided_by: str,
        note: str = "",
        at: dt.datetime | None = None,
    ) -> ReviewTask:
        """Record a steward's decision, applying it if it is a merge."""
        task = self._reviews.get(task_id)
        if task is None:
            raise EmpiError(f"unknown review task {task_id!r}")
        if not task.open:
            raise EmpiError(f"review task {task_id!r} is already decided")
        if not decided_by.strip():
            raise EmpiError("a review decision requires a named reviewer")

        moment = at or dt.datetime.now(dt.UTC)
        if decision is ReviewDecision.SAME_PERSON:
            current = self._person_of_record.get(task.record_id)
            if current and current != task.candidate_person_id:
                self.merge(
                    task.candidate_person_id,
                    current,
                    performed_by=decided_by,
                    reason=note or f"review {task_id}",
                    at=moment,
                )

        resolved = ReviewTask(
            task_id=task.task_id,
            record_id=task.record_id,
            candidate_person_id=task.candidate_person_id,
            score=task.score,
            created_at=task.created_at,
            decision=decision,
            decided_by=decided_by,
            decided_at=moment,
            note=note,
        )
        self._reviews[task_id] = resolved
        return resolved

    def link_history(self, record_id: str) -> tuple[PersonLink, ...]:
        """Every link this record has ever had, oldest first.

        "Why is this record attached to this person" has an answer with a name and a timestamp
        on it, including for links that were later broken.
        """
        found = [link for link in self._links.values() if link.record_id == record_id]
        return tuple(sorted(found, key=lambda link: link.established_at))

    def merges(self) -> tuple[MergeRecord, ...]:
        return tuple(self._merges.values())

    def statistics(self) -> dict[str, Any]:
        return {
            "records": self.record_count(),
            "people": self.person_count(),
            "merges": len(self._merges),
            "reversed_merges": sum(1 for m in self._merges.values() if m.reversed_merge),
            "open_reviews": self.review_queue_depth(),
            "largest_blocking_bucket": self._blocking.bucket_sizes(),
            "degenerate_blocking": self._blocking.degenerate_strategies(),
        }
