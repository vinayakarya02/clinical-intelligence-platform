"""Scheduled report generation.

A report is a dashboard rendered on a schedule and delivered somewhere. Three properties, each
of which is a real operational failure when it is missing.

**A report runs as a principal, and that principal is declared.** A scheduled job with no
identity either runs as root — and quietly emails a compliance dashboard to a distribution list
that should not have it — or runs as whoever last edited it. The subscription names the
principal, and the same scope and disclosure checks apply as for an interactive query.

**A failed run is delivered as a failure, not skipped.** A weekly report that silently stops
arriving is noticed in about a month, and by then the gap is unrecoverable. A run that fails
delivers a notice saying so.

**Delivery is at-least-once, and duplicates are visible.** Making delivery exactly-once needs
coordination this layer does not have; making it at-least-once and stamping every rendering with
its run id means a duplicate is recognisable rather than confusing.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from cip_analytics.boards import DashboardRegistry, DashboardRender
from cip_analytics.domain import AnalyticsError, AnalyticsPrincipal, SchemaError
from cip_analytics.query import QueryExecutor
from cip_core.logging import get_logger

__all__ = [
    "DeliveryChannel",
    "InMemoryDelivery",
    "ReportDefinition",
    "ReportFormat",
    "ReportRun",
    "ReportScheduler",
    "RunStatus",
    "Schedule",
    "ScheduleKind",
]

_log = get_logger(__name__)


class ReportError(AnalyticsError):
    """A report could not be produced or delivered."""


class ScheduleKind(StrEnum):
    """How often a report runs."""

    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    ON_DEMAND = "on_demand"

    @property
    def is_recurring(self) -> bool:
        return self is not ScheduleKind.ON_DEMAND


@dataclass(frozen=True, slots=True)
class Schedule:
    """When a report is due.

    Times are in UTC and that is stated rather than assumed. A "9am" schedule interpreted in
    local time shifts twice a year at daylight saving, and a monthly compliance report that
    lands an hour late twice a year is a finding somebody has to explain.
    """

    kind: ScheduleKind
    hour_utc: int = 6
    day_of_week: int = 0
    """0 = Monday. Used by ``WEEKLY``."""
    day_of_month: int = 1

    def __post_init__(self) -> None:
        if not 0 <= self.hour_utc <= 23:
            raise SchemaError("hour_utc must be between 0 and 23")
        if not 0 <= self.day_of_week <= 6:
            raise SchemaError("day_of_week must be between 0 (Monday) and 6")
        if not 1 <= self.day_of_month <= 28:
            # Capped at 28 deliberately: a schedule for the 31st does not run in February, and
            # a monthly report that silently skips a month is the failure this whole module is
            # about.
            raise SchemaError(
                "day_of_month must be between 1 and 28; later days do not exist in every "
                "month and a report that skips February is worse than one that runs early"
            )

    def is_due(self, now: dt.datetime, last_run: dt.datetime | None) -> bool:
        """Whether a run is due at ``now``.

        Compares against the last run rather than against a cron expression, so a scheduler that
        was down over a due time still fires once when it comes back — a missed compliance
        report is worse than a late one.
        """
        if self.kind is ScheduleKind.ON_DEMAND:
            return False
        if now.hour < self.hour_utc:
            return False
        if last_run is None:
            return True

        if self.kind is ScheduleKind.DAILY:
            return last_run.date() < now.date()
        if self.kind is ScheduleKind.WEEKLY:
            if now.weekday() != self.day_of_week:
                # Catch-up: if a weekly run was missed entirely, fire on any later day rather
                # than waiting a full week for the weekday to come round again.
                return (now.date() - last_run.date()).days >= 7
            return last_run.date() < now.date()
        if now.day < self.day_of_month:
            return False
        return (last_run.year, last_run.month) < (now.year, now.month)

    def describe(self) -> str:
        if self.kind is ScheduleKind.ON_DEMAND:
            return "on demand"
        if self.kind is ScheduleKind.DAILY:
            return f"daily at {self.hour_utc:02d}:00 UTC"
        if self.kind is ScheduleKind.WEEKLY:
            day = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][self.day_of_week]
            return f"weekly on {day} at {self.hour_utc:02d}:00 UTC"
        return f"monthly on day {self.day_of_month} at {self.hour_utc:02d}:00 UTC"


class ReportFormat(StrEnum):
    """How a report is rendered."""

    MARKDOWN = "markdown"
    JSON = "json"
    CSV = "csv"

    @property
    def media_type(self) -> str:
        return {
            "markdown": "text/markdown",
            "json": "application/json",
            "csv": "text/csv",
        }[self.value]


@dataclass(frozen=True, slots=True)
class ReportDefinition:
    """A scheduled rendering of a dashboard."""

    key: str
    title: str
    dashboard_key: str
    schedule: Schedule
    principal: AnalyticsPrincipal
    """Who the report runs as. Declared, so a scheduled job cannot quietly acquire more access
    than the person who set it up."""
    formats: tuple[ReportFormat, ...] = (ReportFormat.MARKDOWN,)
    recipients: tuple[str, ...] = ()
    parameters: dict[str, Any] = field(default_factory=dict)
    description: str = ""
    enabled: bool = True

    def __post_init__(self) -> None:
        if not self.key.strip():
            raise SchemaError("report key must not be empty")
        if not self.formats:
            raise SchemaError(f"report {self.key!r} declares no output format")
        if self.schedule.kind.is_recurring and not self.recipients:
            raise SchemaError(
                f"report {self.key!r} is scheduled but has no recipients; a recurring report "
                "nobody receives is a job that burns resource and is never missed when it breaks"
            )


class RunStatus(StrEnum):
    """How a report run ended."""

    SUCCEEDED = "succeeded"
    DEGRADED = "degraded"
    """Produced, but one or more tiles failed or were stale. Distinct from success because the
    recipient must know the report is incomplete, and distinct from failure because what did
    render is still useful."""
    FAILED = "failed"

    @property
    def was_delivered(self) -> bool:
        return self in (RunStatus.SUCCEEDED, RunStatus.DEGRADED)


@dataclass(frozen=True, slots=True)
class ReportRun:
    """One execution."""

    run_id: str
    report_key: str
    status: RunStatus
    at: dt.datetime
    artifacts: dict[str, str] = field(default_factory=dict)
    failed_tiles: int = 0
    stale_tiles: int = 0
    recipients: tuple[str, ...] = ()
    error: str = ""
    duration_ms: float = 0.0

    def to_json(self) -> dict[str, Any]:
        return {
            "runId": self.run_id,
            "report": self.report_key,
            "status": str(self.status),
            "at": self.at.isoformat(),
            "formats": sorted(self.artifacts),
            "failedTiles": self.failed_tiles,
            "staleTiles": self.stale_tiles,
            "recipients": list(self.recipients),
            "error": self.error,
            "durationMs": round(self.duration_ms, 3),
        }


@runtime_checkable
class DeliveryChannel(Protocol):
    """Where a rendered report goes.

    A protocol with no default implementation. A scheduler that invents a delivery channel when
    none is configured produces reports nobody receives and no error anywhere; a deployment that
    forgot to wire delivery should discover it in configuration.
    """

    name: str

    def deliver(
        self, *, report: ReportDefinition, run: ReportRun, artifacts: dict[str, str]
    ) -> None: ...


@dataclass(slots=True)
class InMemoryDelivery:
    """A delivery channel that records rather than sends.

    Not a stub: it implements the full contract and is what the demo and tests deliver to.
    Sending to a real mailbox or bucket is transport, and transport is the part that is
    deployment-specific.
    """

    name: str = "in-memory"
    delivered: list[tuple[str, str, dict[str, str]]] = field(default_factory=list)
    max_retained: int = 1000

    def deliver(
        self, *, report: ReportDefinition, run: ReportRun, artifacts: dict[str, str]
    ) -> None:
        self.delivered.append((report.key, run.run_id, dict(artifacts)))
        while len(self.delivered) > self.max_retained:
            self.delivered.pop(0)

    def for_report(self, key: str) -> list[tuple[str, str, dict[str, str]]]:
        return [d for d in self.delivered if d[0] == key]


def render_markdown(render: DashboardRender) -> str:
    """A dashboard as Markdown.

    Suppression notes are rendered **with** their table rather than in a footnote. A reader who
    scrolls past a footnote treats the visible cells as the whole picture.
    """
    lines = [f"# {render.dashboard.title}", ""]
    lines.append(f"*{render.dashboard.description}*" if render.dashboard.description else "")
    lines.append(f"\nRendered {render.at.isoformat(timespec='minutes')} UTC.")
    if render.stale_tiles:
        lines.append(f"\n> **{len(render.stale_tiles)} panel(s) show data older than tolerated.**")
    if render.failed_tiles:
        lines.append(f"\n> **{len(render.failed_tiles)} panel(s) could not be produced.**")

    for tile in render.tiles:
        lines.append(f"\n## {tile.tile.title}\n")
        if tile.result is None:
            lines.append(f"*Unavailable: {tile.error}*")
            continue
        result = tile.result
        if result.stale:
            lines.append(f"> Stale: {result.warnings[0] if result.warnings else ''}\n")
        headers = [*result.group_by, f"value ({result.unit})" if result.unit else "value"]
        lines.append("| " + " | ".join(headers) + " |")
        lines.append("|" + "|".join(["---"] * len(headers)) + "|")
        for cell in result.suppression.cells:
            label = [str(g) for g in cell.group] or ["(all)"]
            value = "*suppressed*" if cell.suppressed else f"{cell.value:,.4g}"
            lines.append("| " + " | ".join([*label, value]) + " |")
        if result.suppression.total is not None and not result.suppression.total_suppressed:
            lines.append(
                "| "
                + " | ".join(["**total**"] * len(result.group_by) or ["**total**"])
                + f" | **{result.suppression.total:,.4g}** |"
            )
        if note := result.suppression.note():
            lines.append(f"\n*{note}.*")
        # Lineage and freshness on every table. A report forwarded onward without them is a
        # number with no provenance, and that is what ends up in a slide six months later.
        lineage = result.lineage.to_json()["metric"]
        as_of = result.freshness.to_json()["asOf"] or "freshness unknown"
        lines.append(f"\n<sub>{lineage} — {as_of}</sub>")

    return "\n".join(line for line in lines if line is not None)


def render_csv(render: DashboardRender) -> str:
    """A dashboard as CSV.

    Every row carries its panel and metric, because a CSV loses the structure a dashboard has
    and an unlabelled row of numbers in a spreadsheet is how the wrong figure ends up in a
    slide. Suppressed cells are written empty with a reason column populated — never with their
    value, which is how a suppressed number survives an export.
    """
    rows = ["panel,metric,metric_version,group,value,unit,suppressed,suppression_reason"]
    for tile in render.tiles:
        if tile.result is None:
            rows.append(f'"{tile.tile.title}",,,,,,"true","{tile.error}"')
            continue
        result = tile.result
        for cell in result.suppression.cells:
            group = " / ".join(str(g) for g in cell.group) or "(all)"
            value = "" if cell.suppressed else str(cell.value)
            rows.append(
                f'"{tile.tile.title}","{result.metric_key}","{result.metric_version}",'
                f'"{group}",{value},"{result.unit}","{str(cell.suppressed).lower()}",'
                f'"{cell.reason}"'
            )
    return "\n".join(rows)


def render_json(render: DashboardRender) -> str:
    return json.dumps(render.to_json(), indent=2, sort_keys=True)


_RENDERERS = {
    ReportFormat.MARKDOWN: render_markdown,
    ReportFormat.JSON: render_json,
    ReportFormat.CSV: render_csv,
}


class ReportScheduler:
    """Holds report definitions, decides what is due, and runs them."""

    def __init__(
        self,
        dashboards: DashboardRegistry,
        executor: QueryExecutor,
        delivery: DeliveryChannel,
        *,
        max_runs_retained: int = 2000,
    ) -> None:
        self._dashboards = dashboards
        self._executor = executor
        self._delivery = delivery
        self._reports: dict[str, ReportDefinition] = {}
        self._runs: list[ReportRun] = []
        self._last_run: dict[str, dt.datetime] = {}
        self._max_runs = max_runs_retained

    def register(self, report: ReportDefinition) -> None:
        if report.key in self._reports:
            raise SchemaError(f"report {report.key!r} is already registered")
        if self._dashboards.get(report.dashboard_key) is None:
            raise SchemaError(
                f"report {report.key!r} renders unknown dashboard {report.dashboard_key!r}"
            )
        self._reports[report.key] = report

    def reports(self) -> tuple[ReportDefinition, ...]:
        return tuple(sorted(self._reports.values(), key=lambda r: r.key))

    def due(self, now: dt.datetime) -> tuple[ReportDefinition, ...]:
        return tuple(
            report
            for report in self.reports()
            if report.enabled and report.schedule.is_due(now, self._last_run.get(report.key))
        )

    def run(self, key: str, *, at: dt.datetime | None = None) -> ReportRun:
        """Produce and deliver one report.

        A run that fails still delivers — a failure notice rather than nothing. A report that
        silently stops arriving is noticed in about a month.
        """
        import time

        report = self._reports.get(key)
        if report is None:
            raise ReportError(f"unknown report {key!r}")

        moment = at or dt.datetime.now(dt.UTC)
        started = time.perf_counter()
        run_id = f"run-{uuid.uuid4().hex[:12]}"

        try:
            render = self._dashboards.render(
                report.dashboard_key,
                self._executor,
                report.principal,
                parameters=report.parameters,
                at=moment,
            )
        except Exception as exc:
            run = ReportRun(
                run_id=run_id,
                report_key=key,
                status=RunStatus.FAILED,
                at=moment,
                recipients=report.recipients,
                error=f"{type(exc).__name__}: {exc}",
                duration_ms=(time.perf_counter() - started) * 1000,
            )
            self._record(run)
            self._delivery.deliver(
                report=report,
                run=run,
                artifacts={"text/plain": f"Report {report.title} failed: {run.error}"},
            )
            _log.error("report.failed", report=key, error=run.error[:200])
            return run

        artifacts = {fmt.media_type: _RENDERERS[fmt](render) for fmt in report.formats}
        status = (
            RunStatus.DEGRADED if render.failed_tiles or render.stale_tiles else RunStatus.SUCCEEDED
        )
        run = ReportRun(
            run_id=run_id,
            report_key=key,
            status=status,
            at=moment,
            artifacts=artifacts,
            failed_tiles=len(render.failed_tiles),
            stale_tiles=len(render.stale_tiles),
            recipients=report.recipients,
            duration_ms=(time.perf_counter() - started) * 1000,
        )
        self._record(run)
        self._delivery.deliver(report=report, run=run, artifacts=artifacts)
        self._last_run[key] = moment
        _log.info(
            "report.produced",
            report=key,
            status=status.value,
            failed_tiles=run.failed_tiles,
            stale_tiles=run.stale_tiles,
        )
        return run

    def run_due(self, now: dt.datetime) -> tuple[ReportRun, ...]:
        return tuple(self.run(report.key, at=now) for report in self.due(now))

    def _record(self, run: ReportRun) -> None:
        self._runs.append(run)
        if len(self._runs) > self._max_runs:
            # Failures are retained preferentially: a successful run's output was delivered and
            # exists elsewhere, while a failure exists only here.
            failures = [r for r in self._runs if r.status is RunStatus.FAILED]
            others = [r for r in self._runs if r.status is not RunStatus.FAILED]
            keep = max(0, self._max_runs - len(failures))
            self._runs = failures + others[-keep:] if keep else failures[-self._max_runs :]

    def runs(self, *, report_key: str | None = None) -> tuple[ReportRun, ...]:
        return tuple(r for r in self._runs if report_key is None or r.report_key == report_key)

    def statistics(self) -> dict[str, Any]:
        return {
            "reports": len(self._reports),
            "enabled": sum(1 for r in self._reports.values() if r.enabled),
            "runs": len(self._runs),
            "failed": sum(1 for r in self._runs if r.status is RunStatus.FAILED),
            "degraded": sum(1 for r in self._runs if r.status is RunStatus.DEGRADED),
        }
