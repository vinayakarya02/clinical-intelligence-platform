"""Chronological reconstruction of a patient's record.

A clinical timeline is not a sorted list of rows. Records arrive from different systems with
different date semantics, and three of those differences change what the timeline means:

**Undated records exist and matter.** A condition with no onset is still a condition. Dropping
it produces a timeline that is silently incomplete, which is worse than one that says "date
unknown" — so undated events are collected separately and reported, never discarded.

**Ties are not arbitrary.** Several events on the same day are ordered by clinical
precedence — an encounter precedes what was observed during it, an observation precedes the
medication started in response — so a same-day sequence reads causally rather than by
whichever table was queried first.

**Intervals are not points.** A medication has a start and an end; rendering only the start
loses the fact that it was stopped, which is frequently the clinically decisive event.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field
from enum import StrEnum

from cip_copilot.records import ClinicalDataSource

__all__ = ["Timeline", "TimelineEvent", "TimelineTrack", "build_timeline"]


class TimelineTrack(StrEnum):
    """Which strand of the record an event belongs to."""

    ENCOUNTER = "encounter"
    CONDITION = "condition"
    MEDICATION = "medication"
    OBSERVATION = "observation"


#: Same-day ordering. Lower sorts first. An encounter is the container for what happened
#: during it, and a medication change is usually a response to an observation.
_TRACK_PRECEDENCE: dict[TimelineTrack, int] = {
    TimelineTrack.ENCOUNTER: 0,
    TimelineTrack.CONDITION: 1,
    TimelineTrack.OBSERVATION: 2,
    TimelineTrack.MEDICATION: 3,
}


@dataclass(frozen=True, slots=True)
class TimelineEvent:
    """One dated thing that happened."""

    event_id: str
    track: TimelineTrack
    occurred: dt.date
    summary: str
    ended: dt.date | None = None
    source_ref: str = ""
    detail: dict[str, object] = field(default_factory=dict)

    @property
    def is_interval(self) -> bool:
        return self.ended is not None

    def describe(self) -> str:
        """Render as a dated line, showing the interval when there is one."""
        when = self.occurred.isoformat()
        if self.ended is not None:
            when = f"{when} to {self.ended.isoformat()}"
        return f"{when} — {self.summary}"


@dataclass(frozen=True, slots=True)
class Timeline:
    """An ordered clinical history, plus what could not be placed in it."""

    events: tuple[TimelineEvent, ...] = ()
    undated: tuple[str, ...] = ()
    """Records with no usable date. Surfaced rather than dropped: a timeline that quietly
    omits a diagnosis is a timeline that misleads."""

    def tracks(self) -> set[str]:
        return {str(event.track) for event in self.events}

    def by_track(self, track: TimelineTrack) -> tuple[TimelineEvent, ...]:
        return tuple(event for event in self.events if event.track is track)

    @property
    def span_days(self) -> int | None:
        if not self.events:
            return None
        return (self.events[-1].occurred - self.events[0].occurred).days

    def render(self) -> str:
        """Markdown-ish chronological rendering for an answer body."""
        lines = [event.describe() for event in self.events]
        if self.undated:
            lines.append("")
            lines.append("Undated records:")
            lines.extend(f"- {item}" for item in self.undated)
        return "\n".join(lines)


async def build_timeline(
    source: ClinicalDataSource,
    *,
    patient_id: uuid.UUID,
    tenant_id: uuid.UUID,
    tracks: tuple[str, ...] = (),
) -> Timeline:
    """Assemble a patient's timeline from the structured record.

    ``tracks`` empty means all of them. Narrowing is offered because "show me the medication
    timeline" is a different question from "show me everything", and answering the former with
    the latter buries it.
    """
    wanted = {TimelineTrack(t) for t in tracks} if tracks else set(TimelineTrack)

    events: list[TimelineEvent] = []
    undated: list[str] = []

    if TimelineTrack.ENCOUNTER in wanted:
        for encounter in await source.get_encounters(patient_id, tenant_id=tenant_id):
            events.append(
                TimelineEvent(
                    event_id=f"enc:{encounter.encounter_id}",
                    track=TimelineTrack.ENCOUNTER,
                    occurred=encounter.start,
                    ended=encounter.end,
                    summary=f"{encounter.kind}"
                    + (f" — {encounter.reason}" if encounter.reason else ""),
                    source_ref=f"Encounter/{encounter.encounter_id}",
                )
            )

    if TimelineTrack.CONDITION in wanted:
        for condition in await source.get_conditions(patient_id, tenant_id=tenant_id):
            if condition.onset is None:
                undated.append(f"{condition.display} (no onset date recorded)")
                continue
            events.append(
                TimelineEvent(
                    event_id=f"cond:{condition.condition_id}",
                    track=TimelineTrack.CONDITION,
                    occurred=condition.onset,
                    ended=condition.abatement,
                    summary=f"diagnosed with {condition.display}",
                    source_ref=f"Condition/{condition.condition_id}",
                    detail={"status": condition.clinical_status},
                )
            )

    if TimelineTrack.MEDICATION in wanted:
        for medication in await source.get_medications(patient_id, tenant_id=tenant_id):
            if medication.start_date is None:
                undated.append(f"{medication.display} (no start date recorded)")
                continue
            described = medication.display + (f" {medication.dose}" if medication.dose else "")
            events.append(
                TimelineEvent(
                    event_id=f"med:{medication.medication_id}",
                    track=TimelineTrack.MEDICATION,
                    occurred=medication.start_date,
                    ended=medication.end_date,
                    summary=f"started {described}"
                    if medication.end_date is None
                    else f"{described} (stopped {medication.end_date.isoformat()})",
                    source_ref=f"MedicationStatement/{medication.medication_id}",
                    detail={"status": medication.status},
                )
            )

    if TimelineTrack.OBSERVATION in wanted:
        for observation in await source.get_observations(patient_id, tenant_id=tenant_id):
            events.append(
                TimelineEvent(
                    event_id=f"obs:{observation.observation_id}",
                    track=TimelineTrack.OBSERVATION,
                    occurred=observation.effective,
                    summary=observation.describe(),
                    source_ref=f"Observation/{observation.observation_id}",
                    detail={"flag": observation.flag, "value": observation.value},
                )
            )

    # Sort by date, then clinical precedence, then id — the last so a same-day, same-track
    # pair has a stable order and a rendered timeline does not shuffle between runs.
    events.sort(key=lambda e: (e.occurred, _TRACK_PRECEDENCE[e.track], e.event_id))
    return Timeline(events=tuple(events), undated=tuple(undated))
