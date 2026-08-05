"""Dashboards: the four categories, composed from metric keys.

A dashboard is a **layout**, not a computation. It names metric keys and groupings; it cannot
express an aggregation of its own (docs/design/adr-0034-metric-is-a-definition.md). Two
dashboards showing the same metric key are therefore showing the same number by construction,
which is the property the whole semantic layer exists to provide.

A tile that fails is rendered **as a failed tile with its reason**, not omitted. A dashboard that
silently drops a panel looks complete and is not, and the missing panel is usually the one that
was refused for a disclosure reason somebody needed to know about.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from cip_analytics.domain import AnalyticsPrincipal, SchemaError
from cip_analytics.query import MetricResult, QueryExecutor, QueryRequest
from cip_analytics.semantic import MetricCategory, MetricRegistry
from cip_core.logging import get_logger

__all__ = [
    "Dashboard",
    "DashboardRegistry",
    "DashboardRender",
    "Tile",
    "TileRender",
    "default_dashboards",
]

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Tile:
    """One panel: a template, a metric, and how to group it."""

    title: str
    template_key: str
    group_by: tuple[str, ...] = ()
    parameters: dict[str, Any] = field(default_factory=dict)
    description: str = ""

    def __post_init__(self) -> None:
        if not self.template_key.strip():
            raise SchemaError(f"tile {self.title!r} names no template")


@dataclass(frozen=True, slots=True)
class Dashboard:
    """A named layout over metric keys."""

    key: str
    title: str
    category: MetricCategory
    tiles: tuple[Tile, ...]
    audience: str = ""
    description: str = ""

    def __post_init__(self) -> None:
        if not self.tiles:
            raise SchemaError(f"dashboard {self.key!r} has no tiles")


@dataclass(frozen=True, slots=True)
class TileRender:
    """What one tile produced."""

    tile: Tile
    result: MetricResult | None = None
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.result is not None

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"title": self.tile.title, "ok": self.ok}
        if self.result is not None:
            payload["result"] = self.result.to_json()
        else:
            payload["error"] = self.error
        return payload


@dataclass(frozen=True, slots=True)
class DashboardRender:
    """A whole dashboard, rendered."""

    dashboard: Dashboard
    tiles: tuple[TileRender, ...]
    at: dt.datetime
    duration_ms: float = 0.0

    @property
    def failed_tiles(self) -> tuple[TileRender, ...]:
        return tuple(t for t in self.tiles if not t.ok)

    @property
    def stale_tiles(self) -> tuple[TileRender, ...]:
        return tuple(t for t in self.tiles if t.result is not None and t.result.stale)

    def to_json(self) -> dict[str, Any]:
        return {
            "key": self.dashboard.key,
            "title": self.dashboard.title,
            "category": str(self.dashboard.category),
            "renderedAt": self.at.isoformat(),
            "durationMs": round(self.duration_ms, 3),
            "tiles": [t.to_json() for t in self.tiles],
            "failedTiles": len(self.failed_tiles),
            "staleTiles": len(self.stale_tiles),
        }

    def render(self) -> str:
        lines = [f"=== {self.dashboard.title} ({self.dashboard.category.value}) ==="]
        for tile in self.tiles:
            lines.append(f"\n-- {tile.tile.title}")
            if tile.result is None:
                lines.append(f"   unavailable: {tile.error}")
                continue
            lines.append(tile.result.render())
        if self.stale_tiles:
            lines.append(f"\n! {len(self.stale_tiles)} tile(s) show data older than tolerated")
        return "\n".join(lines)


class DashboardRegistry:
    """The dashboards this deployment publishes."""

    def __init__(self, metrics: MetricRegistry) -> None:
        self._metrics = metrics
        self._dashboards: dict[str, Dashboard] = {}

    def register(self, dashboard: Dashboard) -> None:
        if dashboard.key in self._dashboards:
            raise SchemaError(f"dashboard {dashboard.key!r} is already registered")
        self._dashboards[dashboard.key] = dashboard

    def get(self, key: str) -> Dashboard | None:
        return self._dashboards.get(key)

    def keys(self) -> tuple[str, ...]:
        return tuple(sorted(self._dashboards))

    def by_category(self, category: MetricCategory) -> tuple[Dashboard, ...]:
        return tuple(
            sorted(
                (d for d in self._dashboards.values() if d.category is category),
                key=lambda d: d.key,
            )
        )

    def render(
        self,
        key: str,
        executor: QueryExecutor,
        principal: AnalyticsPrincipal,
        *,
        parameters: dict[str, Any] | None = None,
        at: dt.datetime | None = None,
    ) -> DashboardRender:
        """Render every tile.

        A tile that raises is captured as a failed tile rather than aborting the dashboard. One
        refused panel should not blank the other seven, and the refusal reason — often a
        disclosure or scope refusal — is exactly what the viewer needs to see.
        """
        import time

        dashboard = self._dashboards.get(key)
        if dashboard is None:
            raise SchemaError(f"unknown dashboard {key!r}")

        moment = at or dt.datetime.now(dt.UTC)
        started = time.perf_counter()
        renders: list[TileRender] = []

        for tile in dashboard.tiles:
            merged = {**tile.parameters, **(parameters or {})}
            try:
                result = executor.execute(
                    QueryRequest(
                        template_key=tile.template_key,
                        principal=principal,
                        parameters=merged,
                        group_by=tile.group_by,
                        at=moment,
                    )
                )
                renders.append(TileRender(tile=tile, result=result))
            except Exception as exc:
                renders.append(TileRender(tile=tile, error=f"{type(exc).__name__}: {exc}"))

        render = DashboardRender(
            dashboard=dashboard,
            tiles=tuple(renders),
            at=moment,
            duration_ms=(time.perf_counter() - started) * 1000,
        )
        _log.info(
            "dashboard.rendered",
            dashboard=key,
            tiles=len(renders),
            failed=len(render.failed_tiles),
            stale=len(render.stale_tiles),
        )
        return render


def default_dashboards() -> tuple[Dashboard, ...]:
    """The four categories from the Phase 0 design, unchanged.

    Each tile names a template rather than a metric directly, because the template carries the
    scope and the parameter bounds — and authorisation belongs to what is being asked, not to
    the layout asking it.
    """
    return (
        Dashboard(
            key="clinical-pharmacovigilance",
            title="Clinical & Pharmacovigilance",
            category=MetricCategory.CLINICAL,
            audience="Pharmacovigilance analysts, medical affairs",
            description="Adverse-event signal, cohort sizing, and result abnormality.",
            tiles=(
                Tile(
                    title="Adverse events by month",
                    template_key="adverse_event_trend",
                    group_by=("dim_date.month",),
                ),
                Tile(
                    title="Adverse events by drug",
                    template_key="adverse_event_trend",
                    group_by=("dim_code.display",),
                ),
                Tile(
                    title="Serious event proportion by quarter",
                    template_key="serious_event_rate",
                    group_by=("dim_date.quarter",),
                ),
                Tile(
                    title="Distinct patients with observations, by month",
                    template_key="cohort_size",
                    group_by=("dim_date.month",),
                ),
                Tile(
                    title="Abnormal result rate by analyte",
                    template_key="abnormal_rate",
                    group_by=("dim_code.display",),
                ),
            ),
        ),
        Dashboard(
            key="operational",
            title="Platform Operations",
            category=MetricCategory.OPERATIONAL,
            audience="Platform and data engineering",
            description="Ingestion throughput, quality, latency, and job health.",
            tiles=(
                Tile(
                    title="Documents ingested by source",
                    template_key="ingestion_volume",
                    group_by=("dim_source_system.source_system",),
                ),
                Tile(
                    title="Ingestion success rate by source",
                    template_key="ingestion_quality",
                    group_by=("dim_source_system.source_system",),
                ),
                Tile(
                    title="Ingestion p95 latency by source",
                    template_key="ingestion_latency",
                    group_by=("dim_source_system.source_system",),
                ),
                Tile(
                    title="Mean extraction quality by document type",
                    template_key="extraction_quality",
                    group_by=("document_type",),
                ),
                Tile(
                    title="Failed job runs by kind",
                    template_key="job_failures",
                    group_by=("job_kind",),
                ),
            ),
        ),
        Dashboard(
            key="governance",
            title="Governance & Compliance",
            category=MetricCategory.GOVERNANCE,
            audience="Compliance and security officers",
            description="Disclosure decisions, break-glass review, de-identification jobs.",
            tiles=(
                Tile(
                    title="Disclosure decisions by outcome",
                    template_key="phi_access_summary",
                    group_by=("outcome",),
                ),
                Tile(
                    title="Disclosure decisions by purpose",
                    template_key="phi_access_summary",
                    group_by=("purpose",),
                ),
                Tile(
                    title="Break-glass accesses by month",
                    template_key="break_glass_summary",
                    group_by=("dim_date.month",),
                ),
                Tile(
                    title="Break-glass review rate",
                    template_key="break_glass_review",
                    group_by=("dim_date.month",),
                    description="The number that says whether the break-glass control is real.",
                ),
                Tile(
                    title="De-identification job runs by status",
                    template_key="deid_job_status",
                    group_by=("status",),
                ),
            ),
        ),
        Dashboard(
            key="usage",
            title="Adoption & Answer Quality",
            category=MetricCategory.USAGE,
            audience="Product, customer success",
            description="Volume, grounding, abstention, and latency.",
            tiles=(
                Tile(
                    title="Answers by month",
                    template_key="answer_volume",
                    group_by=("dim_date.month",),
                ),
                Tile(
                    title="Answers by question category",
                    template_key="answer_volume",
                    group_by=("question_category",),
                ),
                Tile(
                    title="Grounding pass rate by month",
                    template_key="grounding_rate",
                    group_by=("dim_date.month",),
                ),
                Tile(
                    title="Abstention rate by month",
                    template_key="abstention",
                    group_by=("dim_date.month",),
                    description="Shown beside grounding: a system that answers everything "
                    "scores well on one and badly on the other.",
                ),
                Tile(
                    title="Answer p95 latency by category",
                    template_key="answer_latency",
                    group_by=("question_category",),
                ),
            ),
        ),
    )
