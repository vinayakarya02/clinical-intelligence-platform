"""The analytics API.

Implements the surface declared in Phase 0's OpenAPI specification — ``/analytics/metrics/{key}``
and ``/analytics/reports`` — with the property that specification insisted on: parameterised,
RBAC-scoped templates only, no free-form SQL or Cypher
(docs/design/adr-0035-no-free-form-queries.md).

Transport-agnostic. It returns status codes and bodies rather than framework responses, so the
same contract is testable without standing up a web server and the gateway can mount it however
it mounts everything else. That is the same seam Phase 6's clinical API uses.

Every refusal names its cause. "403" tells an analyst nothing; "this template permits grouping
by month or drug, not by postal prefix" tells them what to ask instead, and the second costs a
support ticket less.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from cip_analytics.boards import DashboardRegistry
from cip_analytics.domain import (
    AnalyticsPrincipal,
    QueryError,
    SchemaError,
    SuppressionError,
)
from cip_analytics.query import QueryExecutor, QueryRequest, TemplateRegistry
from cip_analytics.reports import ReportScheduler
from cip_analytics.semantic import MetricRegistry
from cip_core.logging import get_logger

__all__ = ["AnalyticsApi", "ApiResponse"]

_log = get_logger(__name__)

API_VERSION = "v1"


@dataclass(frozen=True, slots=True)
class ApiResponse:
    """One response."""

    status: int
    body: dict[str, Any] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def render(self) -> str:
        import json

        return f"{self.status} {json.dumps(self.body, sort_keys=True)[:200]}"


def _problem(status: int, code: str, detail: str, **extra: Any) -> ApiResponse:
    """A refusal that says what to do about it.

    Shaped after RFC 9457 problem details, because an analytics API is consumed by people
    writing integrations and a bare status code costs them a support ticket each.
    """
    body: dict[str, Any] = {"type": f"urn:cip:analytics:{code}", "title": code, "detail": detail}
    body.update(extra)
    return ApiResponse(status=status, body=body)


class AnalyticsApi:
    """The read-only analytics surface.

    Read-only by construction: there is no write path here at all. Facts arrive through the ETL
    and nowhere else, which is what makes the warehouse reproducible from its sources.
    """

    def __init__(
        self,
        *,
        executor: QueryExecutor,
        metrics: MetricRegistry,
        templates: TemplateRegistry,
        dashboards: DashboardRegistry,
        scheduler: ReportScheduler | None = None,
    ) -> None:
        self._executor = executor
        self._metrics = metrics
        self._templates = templates
        self._dashboards = dashboards
        self._scheduler = scheduler

    def get_metric(
        self,
        metric_key: str,
        principal: AnalyticsPrincipal,
        *,
        parameters: dict[str, Any] | None = None,
        group_by: tuple[str, ...] = (),
        at: dt.datetime | None = None,
    ) -> ApiResponse:
        """``GET /analytics/metrics/{metricKey}``.

        The path parameter is a **metric** key, as the specification says. The template is
        resolved from it, so a caller cannot select a more permissive template for a metric than
        the one its authors intended.
        """
        metric = self._metrics.get(metric_key)
        if metric is None:
            return _problem(
                404,
                "unknown-metric",
                f"no metric {metric_key!r} is defined",
                available=list(self._metrics.keys()),
            )

        template_key = self._template_for(metric_key)
        if template_key is None:
            return _problem(
                404,
                "no-template",
                f"metric {metric_key!r} is defined but no query template exposes it; a metric "
                "without a template cannot be queried, which is deliberate",
            )

        try:
            result = self._executor.execute(
                QueryRequest(
                    template_key=template_key,
                    principal=principal,
                    parameters=parameters or {},
                    group_by=group_by,
                    at=at or dt.datetime.now(dt.UTC),
                )
            )
        except SuppressionError as exc:
            # 409: the request is well-formed and authorised, and the *result* cannot be
            # published safely. A different grouping or a wider range will often succeed, so the
            # caller is told that rather than being told they are forbidden.
            return _problem(
                409,
                "cannot-publish-safely",
                str(exc),
                hint="widen the date range or group less finely",
            )
        except QueryError as exc:
            message = str(exc)
            status = 403 if "scope" in message else 400
            return _problem(status, "invalid-query" if status == 400 else "forbidden", message)
        except SchemaError as exc:
            return _problem(400, "invalid-query", str(exc))

        return ApiResponse(
            status=200,
            body=result.to_json(),
            headers={
                "Cache-Control": "no-store",
                # Freshness in a header as well as the body: a caching proxy or a dashboard
                # framework that only reads headers still learns the data's age.
                "X-Data-As-Of": result.freshness.to_json()["asOf"] or "unknown",
                "X-Metric-Version": result.metric_version,
            },
        )

    def _template_for(self, metric_key: str) -> str | None:
        for key in self._templates.keys():  # noqa: SIM118 - a registry, not a dict
            template = self._templates.get(key)
            if template is not None and template.metric_key == metric_key:
                return key
        return None

    def list_metrics(self, principal: AnalyticsPrincipal) -> ApiResponse:
        """The catalogue of what may be asked.

        A governance artifact as much as a developer convenience: "what can be computed from
        this data" is a question a compliance officer needs answered by enumeration.
        """
        visible = [
            entry
            for entry in self._metrics.catalogue()
            if not entry["requiresElevatedScope"] or "analytics:elevated" in principal.scopes
        ]
        return ApiResponse(
            status=200,
            body={"metrics": visible, "count": len(visible)},
        )

    def list_templates(self, principal: AnalyticsPrincipal) -> ApiResponse:
        return ApiResponse(
            status=200,
            body={
                "templates": [
                    t for t in self._templates.catalogue() if t["requiredScope"] in principal.scopes
                ]
            },
        )

    def get_dashboard(
        self,
        key: str,
        principal: AnalyticsPrincipal,
        *,
        parameters: dict[str, Any] | None = None,
        at: dt.datetime | None = None,
    ) -> ApiResponse:
        dashboard = self._dashboards.get(key)
        if dashboard is None:
            return _problem(
                404,
                "unknown-dashboard",
                f"no dashboard {key!r}",
                available=list(self._dashboards.keys()),
            )
        try:
            render = self._dashboards.render(
                key, self._executor, principal, parameters=parameters, at=at
            )
        except SchemaError as exc:
            return _problem(400, "invalid-request", str(exc))
        # 200 even when tiles failed. The failures are in the body per tile, because a
        # dashboard where seven of eight panels rendered is useful and a blanket error is not.
        return ApiResponse(status=200, body=render.to_json())

    def list_dashboards(self, principal: AnalyticsPrincipal) -> ApiResponse:
        del principal
        return ApiResponse(
            status=200,
            body={
                "dashboards": [
                    {
                        "key": key,
                        "title": board.title,
                        "category": str(board.category),
                        "audience": board.audience,
                        "tiles": len(board.tiles),
                    }
                    for key in self._dashboards.keys()  # noqa: SIM118
                    if (board := self._dashboards.get(key)) is not None
                ]
            },
        )

    def list_reports(self, principal: AnalyticsPrincipal) -> ApiResponse:
        """``GET /analytics/reports``."""
        if self._scheduler is None:
            return ApiResponse(status=200, body={"reports": []})
        return ApiResponse(
            status=200,
            body={
                "reports": [
                    {
                        "key": report.key,
                        "title": report.title,
                        "dashboard": report.dashboard_key,
                        "schedule": report.schedule.describe(),
                        "formats": [str(f) for f in report.formats],
                        "enabled": report.enabled,
                        # Recipients are shown only to the principal who owns the report.
                        # A subscriber list is a directory of who reads compliance data.
                        "recipients": list(report.recipients)
                        if report.principal.principal_id == principal.principal_id
                        else None,
                    }
                    for report in self._scheduler.reports()
                ]
            },
        )

    def get_report_runs(self, key: str, principal: AnalyticsPrincipal) -> ApiResponse:
        if self._scheduler is None:
            return _problem(404, "no-scheduler", "no report scheduler is configured")
        runs = self._scheduler.runs(report_key=key)
        if not runs:
            return _problem(404, "unknown-report", f"no runs recorded for report {key!r}")
        del principal
        return ApiResponse(status=200, body={"runs": [r.to_json() for r in runs[-50:]]})

    def health(self) -> ApiResponse:
        """Readiness, including whether the warehouse has been loaded at all.

        An analytics service that is up but has an empty warehouse answers every question with
        zero, and zero reads as a real finding. Reporting the load state separately from process
        health is what stops that being invisible.
        """
        loaded = self._executor.warehouse.statistics()
        has_data = bool(loaded.get("facts"))
        return ApiResponse(
            status=200 if has_data else 503,
            body={
                "status": "ready" if has_data else "warehouse-empty",
                "warehouse": loaded,
                "metrics": self._metrics.count(),
                "templates": len(self._templates.keys()),
                "dashboards": len(self._dashboards.keys()),
                "detail": ""
                if has_data
                else "the warehouse holds no facts; every metric would answer zero",
            },
        )
