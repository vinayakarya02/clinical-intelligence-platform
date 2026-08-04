"""Real-time dashboard infrastructure.

Windowed aggregation over the event stream, projected into four audiences. The audiences are
not cosmetic: an operational dashboard shows interface lag and dead-letter depth, a clinical one
shows results pending, and an executive one shows neither. Building one dashboard and filtering
it per role is how PHI ends up on a screen in a boardroom.

**No metric here carries a patient identifier.** Counts, rates, and latencies only. A dashboard
is displayed on shared screens and retained in a metrics store with weaker access controls than
the record, so a per-patient metric is a disclosure with a chart around it.
"""

from __future__ import annotations

import datetime as dt
from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from cip_interop.streaming import ClinicalEventType, StreamRecord

__all__ = [
    "Audience",
    "DashboardRegistry",
    "MetricWindow",
    "Panel",
    "Snapshot",
]


class Audience(StrEnum):
    """Who a dashboard is for.

    Separate projections rather than one filtered view, because the failure mode of filtering
    is that a new metric is visible to everyone until somebody remembers to restrict it.
    """

    OPERATIONAL = "operational"
    """Interface health: throughput, lag, dead letters, ordering violations."""
    CLINICAL = "clinical"
    """Care delivery: results pending, orders outstanding, referrals open."""
    HOSPITAL = "hospital"
    """Facility flow: admissions, discharges, occupancy proxies."""
    EXECUTIVE = "executive"
    """Aggregate volume and reliability, no operational detail."""

    @property
    def may_show_patient_counts(self) -> bool:
        """Whether small counts are meaningful here.

        Executive dashboards get rounded aggregates; a count of three on a boardroom screen is
        a small-cell disclosure in a room full of people.
        """
        return self in (Audience.OPERATIONAL, Audience.CLINICAL, Audience.HOSPITAL)


@dataclass(slots=True)
class MetricWindow:
    """A sliding time window of observations.

    Bounded by both time and count. Time alone is not enough: a burst can put millions of
    observations inside a five-minute window, and a dashboard that runs the process out of
    memory is a worse outage than the one it was displaying.
    """

    window: dt.timedelta = dt.timedelta(minutes=5)
    capacity: int = 50_000
    _points: deque[tuple[dt.datetime, float]] = field(default_factory=deque)

    def observe(self, value: float = 1.0, *, at: dt.datetime | None = None) -> None:
        moment = at or dt.datetime.now(dt.UTC)
        self._points.append((moment, value))
        while len(self._points) > self.capacity:
            self._points.popleft()

    def _trim(self, now: dt.datetime) -> None:
        cutoff = now - self.window
        while self._points and self._points[0][0] < cutoff:
            self._points.popleft()

    def count(self, *, now: dt.datetime | None = None) -> int:
        moment = now or dt.datetime.now(dt.UTC)
        self._trim(moment)
        return len(self._points)

    def total(self, *, now: dt.datetime | None = None) -> float:
        moment = now or dt.datetime.now(dt.UTC)
        self._trim(moment)
        return sum(v for _, v in self._points)

    def rate_per_minute(self, *, now: dt.datetime | None = None) -> float:
        moment = now or dt.datetime.now(dt.UTC)
        observations = self.count(now=moment)
        minutes = self.window.total_seconds() / 60
        return round(observations / minutes, 3) if minutes else 0.0

    def percentile(self, fraction: float, *, now: dt.datetime | None = None) -> float:
        moment = now or dt.datetime.now(dt.UTC)
        self._trim(moment)
        if not self._points:
            return 0.0
        values = sorted(v for _, v in self._points)
        index = min(len(values) - 1, max(0, int(len(values) * fraction) - 1))
        return values[index]


@dataclass(frozen=True, slots=True)
class Panel:
    """One number on a dashboard."""

    key: str
    label: str
    value: float | int | None
    unit: str = ""
    audience: Audience = Audience.OPERATIONAL
    warning_above: float | None = None

    @property
    def breaching(self) -> bool:
        return (
            self.warning_above is not None
            and self.value is not None
            and self.value > self.warning_above
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "value": self.value,
            "unit": self.unit,
            "breaching": self.breaching,
        }


@dataclass(frozen=True, slots=True)
class Snapshot:
    """One rendering of a dashboard."""

    audience: Audience
    panels: tuple[Panel, ...]
    at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))

    @property
    def breaches(self) -> tuple[Panel, ...]:
        return tuple(p for p in self.panels if p.breaching)

    def render(self) -> str:
        lines = [f"[{self.audience.value}]"]
        for panel in self.panels:
            flag = " !" if panel.breaching else ""
            value = "n/a" if panel.value is None else f"{panel.value:,.2f}".rstrip("0").rstrip(".")
            lines.append(f"  {panel.label:<34} {value:>12} {panel.unit}{flag}")
        return "\n".join(lines)

    def to_json(self) -> dict[str, Any]:
        return {
            "audience": str(self.audience),
            "at": self.at.isoformat(),
            "panels": [p.to_json() for p in self.panels],
        }


class DashboardRegistry:
    """Consumes stream records and projects them into audience dashboards."""

    def __init__(self, *, window: dt.timedelta = dt.timedelta(minutes=5)) -> None:
        self._window = window
        self._by_type: dict[ClinicalEventType, MetricWindow] = {}
        self._by_organization: dict[str, MetricWindow] = {}
        self._latency = MetricWindow(window=window)
        self._total = 0
        self._ordering_violations = 0
        self._dead_letters = 0
        self._break_glass = 0
        self._review_queue_depth = 0
        self._consumer_lag = 0

    def observe(self, record: StreamRecord, *, at: dt.datetime | None = None) -> None:
        """Count one stream record.

        Only the type and the organisation are read. The payload is never touched — a metrics
        pipeline that reads payloads is one PHI leak away from a metrics store nobody audits.
        """
        moment = at or dt.datetime.now(dt.UTC)
        self._total += 1
        self._by_type.setdefault(record.event_type, MetricWindow(window=self._window)).observe(
            at=moment
        )
        if record.organization_id:
            self._by_organization.setdefault(
                record.organization_id, MetricWindow(window=self._window)
            ).observe(at=moment)

    def observe_latency(self, milliseconds: float, *, at: dt.datetime | None = None) -> None:
        self._latency.observe(milliseconds, at=at)

    def set_operational(
        self,
        *,
        ordering_violations: int | None = None,
        dead_letters: int | None = None,
        break_glass: int | None = None,
        review_queue_depth: int | None = None,
        consumer_lag: int | None = None,
    ) -> None:
        if ordering_violations is not None:
            self._ordering_violations = ordering_violations
        if dead_letters is not None:
            self._dead_letters = dead_letters
        if break_glass is not None:
            self._break_glass = break_glass
        if review_queue_depth is not None:
            self._review_queue_depth = review_queue_depth
        if consumer_lag is not None:
            self._consumer_lag = consumer_lag

    def _rate(self, event_type: ClinicalEventType, now: dt.datetime) -> float:
        window = self._by_type.get(event_type)
        return window.rate_per_minute(now=now) if window else 0.0

    def snapshot(self, audience: Audience, *, now: dt.datetime | None = None) -> Snapshot:
        moment = now or dt.datetime.now(dt.UTC)

        if audience is Audience.OPERATIONAL:
            panels = (
                Panel("events_total", "Events processed", self._total, "events", audience),
                Panel(
                    "ingest_rate",
                    "Ingest rate",
                    round(sum(w.rate_per_minute(now=moment) for w in self._by_type.values()), 2),
                    "/min",
                    audience,
                ),
                Panel(
                    "p95_latency",
                    "Ingest p95",
                    self._latency.percentile(0.95, now=moment),
                    "ms",
                    audience,
                    warning_above=500,
                ),
                Panel(
                    "dead_letters",
                    "Dead letters",
                    self._dead_letters,
                    "",
                    audience,
                    warning_above=0,
                ),
                Panel(
                    "ordering_violations",
                    "Ordering violations",
                    self._ordering_violations,
                    "",
                    audience,
                    warning_above=0,
                ),
                Panel(
                    "consumer_lag",
                    "Consumer lag",
                    self._consumer_lag,
                    "records",
                    audience,
                    warning_above=1000,
                ),
                Panel(
                    "empi_review_queue",
                    "EMPI review queue",
                    self._review_queue_depth,
                    "",
                    audience,
                    warning_above=50,
                ),
                Panel(
                    "break_glass",
                    "Break-glass accesses",
                    self._break_glass,
                    "",
                    audience,
                    warning_above=0,
                ),
            )
        elif audience is Audience.CLINICAL:
            panels = (
                Panel(
                    "results_rate",
                    "Results completed",
                    self._rate(ClinicalEventType.LAB_RESULT_COMPLETED, moment),
                    "/min",
                    audience,
                ),
                Panel(
                    "orders_rate",
                    "Orders placed",
                    self._rate(ClinicalEventType.LAB_ORDER_PLACED, moment),
                    "/min",
                    audience,
                ),
                Panel(
                    "imaging_rate",
                    "Imaging available",
                    self._rate(ClinicalEventType.IMAGING_STUDY_AVAILABLE, moment),
                    "/min",
                    audience,
                ),
                Panel(
                    "referrals_rate",
                    "Referrals initiated",
                    self._rate(ClinicalEventType.REFERRAL_INITIATED, moment),
                    "/min",
                    audience,
                ),
                Panel(
                    "decisions_rate",
                    "Decisions generated",
                    self._rate(ClinicalEventType.DECISION_GENERATED, moment),
                    "/min",
                    audience,
                ),
            )
        elif audience is Audience.HOSPITAL:
            panels = (
                Panel(
                    "admissions",
                    "Admissions",
                    self._rate(ClinicalEventType.PATIENT_ADMITTED, moment),
                    "/min",
                    audience,
                ),
                Panel(
                    "discharges",
                    "Discharges",
                    self._rate(ClinicalEventType.PATIENT_DISCHARGED, moment),
                    "/min",
                    audience,
                ),
                Panel(
                    "transfers",
                    "Transfers",
                    self._rate(ClinicalEventType.PATIENT_TRANSFERRED, moment),
                    "/min",
                    audience,
                ),
                Panel(
                    "organizations",
                    "Contributing organisations",
                    len(self._by_organization),
                    "",
                    audience,
                ),
            )
        else:
            # Executive: aggregate volume and reliability only. Rounded, and with no per-event
            # or per-organisation breakdown, because this view is the one shown on a wall.
            panels = (
                Panel("events_total", "Total events", self._total, "", audience),
                Panel(
                    "organizations",
                    "Connected organisations",
                    len(self._by_organization),
                    "",
                    audience,
                ),
                Panel(
                    "reliability",
                    "Delivered without dead-letter",
                    round(
                        100.0 * (self._total - self._dead_letters) / self._total,
                        2,
                    )
                    if self._total
                    else None,
                    "%",
                    audience,
                ),
            )

        return Snapshot(audience=audience, panels=panels, at=moment)

    def all_snapshots(self, *, now: dt.datetime | None = None) -> tuple[Snapshot, ...]:
        return tuple(self.snapshot(a, now=now) for a in Audience)
