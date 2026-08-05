"""The query layer: typed templates, execution, and disclosure control.

There is no path from a caller's string to a query plan
(docs/design/adr-0035-no-free-form-queries.md). A caller names a metric and supplies typed,
bounded parameters; anything else is refused. Nothing here parses SQL, so there is nothing to
sanitise.

The restriction that is *not* about injection is the grouping bound. A valid, parameterised,
perfectly safe query grouping by three-digit postal, birth year, and sex singles out a person
with no injection at all, so the number of quasi-identifying dimensions in one request is
capped regardless of who is asking.

Disclosure control runs **here**, in the executor, rather than in any display layer — suppression
is a property of the whole result set and a chart only ever sees its own slice
(docs/design/adr-0036-complementary-suppression.md).
"""

from __future__ import annotations

import datetime as dt
import statistics
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from cip_analytics.disclosure import (
    Cell,
    QuasiIdentifierBudget,
    SuppressionOutcome,
    apply_disclosure_control,
)
from cip_analytics.domain import (
    ANALYTICS_ELEVATED,
    AnalyticsPrincipal,
    Freshness,
    MeasureKind,
    QueryError,
    SchemaError,
    Scope,
    Sensitivity,
)
from cip_analytics.semantic import MetricDefinition, MetricLineage, MetricRegistry
from cip_analytics.warehouse import Warehouse
from cip_core.logging import get_logger

__all__ = [
    "MetricResult",
    "ParameterType",
    "QueryExecutor",
    "QueryRequest",
    "QueryTemplate",
    "TemplateParameter",
    "TemplateRegistry",
]

_log = get_logger(__name__)

#: A query may never scan more than this many days. An unbounded range is both a
#: denial-of-service and, on a small population, a re-identification aid.
_ABSOLUTE_MAX_RANGE_DAYS = 3660


class ParameterType(StrEnum):
    """The types a template parameter may declare."""

    DATE = "date"
    INTEGER = "integer"
    DECIMAL = "decimal"
    ENUM = "enum"
    DIMENSION = "dimension"
    """A grouping dimension, validated against the template's permitted set."""

    @property
    def is_grouping(self) -> bool:
        return self is ParameterType.DIMENSION


@dataclass(frozen=True, slots=True)
class TemplateParameter:
    """One declared parameter.

    Every constraint is declared here rather than checked at the call site, so the contract is
    enumerable — which is what makes the template registry a governance artifact rather than a
    developer convenience.
    """

    name: str
    type: ParameterType
    required: bool = False
    allowed: frozenset[str] = frozenset()
    minimum: float | None = None
    maximum: float | None = None
    description: str = ""

    def coerce(self, raw: Any) -> Any:
        """Validate and convert. Raises :class:`QueryError` with the reason."""
        if self.type is ParameterType.DATE:
            if isinstance(raw, dt.date):
                return raw
            try:
                return dt.date.fromisoformat(str(raw))
            except ValueError as exc:
                raise QueryError(
                    f"parameter {self.name!r} must be an ISO date (YYYY-MM-DD), got {raw!r}"
                ) from exc
        if self.type is ParameterType.INTEGER:
            try:
                value = int(raw)
            except (TypeError, ValueError) as exc:
                raise QueryError(f"parameter {self.name!r} must be an integer") from exc
            return self._bounded(value)
        if self.type is ParameterType.DECIMAL:
            try:
                value = float(raw)
            except (TypeError, ValueError) as exc:
                raise QueryError(f"parameter {self.name!r} must be a number") from exc
            return self._bounded(value)
        text = str(raw)
        if self.allowed and text not in self.allowed:
            raise QueryError(
                f"parameter {self.name!r} must be one of {sorted(self.allowed)}, got {text!r}"
            )
        return text

    def _bounded(self, value: float) -> float | int:
        if self.minimum is not None and value < self.minimum:
            raise QueryError(f"parameter {self.name!r} must be at least {self.minimum}")
        if self.maximum is not None and value > self.maximum:
            raise QueryError(f"parameter {self.name!r} must be at most {self.maximum}")
        return value


@dataclass(frozen=True, slots=True)
class QueryTemplate:
    """A named, parameterised, scoped question.

    The scope lives on the template rather than at the call site, so authorisation is a property
    of what is being asked and cannot be forgotten by whoever adds the next endpoint.
    """

    key: str
    metric_key: str
    required_scope: Scope
    parameters: tuple[TemplateParameter, ...] = ()
    permitted_group_by: frozenset[str] = frozenset()
    max_group_by: int = 2
    max_range_days: int = 400
    description: str = ""

    def __post_init__(self) -> None:
        if self.max_range_days > _ABSOLUTE_MAX_RANGE_DAYS:
            raise SchemaError(
                f"template {self.key!r} permits a {self.max_range_days}-day range, above the "
                f"absolute cap of {_ABSOLUTE_MAX_RANGE_DAYS}"
            )
        if self.max_group_by < 0:
            raise SchemaError(f"template {self.key!r} has a negative max_group_by")
        names = [p.name for p in self.parameters]
        if len(names) != len(set(names)):
            raise SchemaError(f"template {self.key!r} declares a parameter twice")

    def parameter(self, name: str) -> TemplateParameter | None:
        return next((p for p in self.parameters if p.name == name), None)

    def to_json(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "metric": self.metric_key,
            "requiredScope": str(self.required_scope),
            "parameters": [
                {
                    "name": p.name,
                    "type": str(p.type),
                    "required": p.required,
                    "allowed": sorted(p.allowed) if p.allowed else None,
                }
                for p in self.parameters
            ],
            "permittedGroupBy": sorted(self.permitted_group_by),
            "maxGroupBy": self.max_group_by,
            "maxRangeDays": self.max_range_days,
        }


class TemplateRegistry:
    """Every question that may be asked of this warehouse."""

    def __init__(self, metrics: MetricRegistry) -> None:
        self._metrics = metrics
        self._templates: dict[str, QueryTemplate] = {}

    def register(self, template: QueryTemplate) -> None:
        if template.key in self._templates:
            raise SchemaError(f"template {template.key!r} is already registered")
        existing = next(
            (t for t in self._templates.values() if t.metric_key == template.metric_key), None
        )
        if existing is not None:
            # One template per metric. With two, a caller naming the *metric* gets whichever the
            # lookup happens to find first — and if their scopes differ that is a silent
            # privilege escalation to the more permissive one.
            raise SchemaError(
                f"metric {template.metric_key!r} is already exposed by template "
                f"{existing.key!r}. Two templates for one metric means a caller naming the "
                "metric gets whichever is found first, which escalates to the more permissive "
                "scope. Vary the metric, not the template."
            )
        metric = self._metrics.get(template.metric_key)
        if metric is None:
            raise SchemaError(
                f"template {template.key!r} references unknown metric {template.metric_key!r}"
            )
        for dimension in template.permitted_group_by:
            if not metric.permits_group_by(dimension):
                raise SchemaError(
                    f"template {template.key!r} permits grouping by {dimension!r}, which metric "
                    f"{metric.key!r} does not allow. The metric is the narrower authority."
                )
        self._templates[template.key] = template

    def get(self, key: str) -> QueryTemplate | None:
        return self._templates.get(key)

    def keys(self) -> tuple[str, ...]:
        return tuple(sorted(self._templates))

    def catalogue(self) -> list[dict[str, Any]]:
        return [t.to_json() for t in sorted(self._templates.values(), key=lambda t: t.key)]


@dataclass(frozen=True, slots=True)
class QueryRequest:
    """One call against a template."""

    template_key: str
    principal: AnalyticsPrincipal
    parameters: dict[str, Any] = field(default_factory=dict)
    group_by: tuple[str, ...] = ()
    at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))


@dataclass(frozen=True, slots=True)
class MetricResult:
    """An answer, with everything needed to trust and reproduce it."""

    metric_key: str
    metric_version: str
    title: str
    unit: str
    group_by: tuple[str, ...]
    suppression: SuppressionOutcome
    freshness: Freshness
    lineage: MetricLineage
    rows_scanned: int
    duration_ms: float
    stale: bool = False
    warnings: tuple[str, ...] = ()

    @property
    def published_rows(self) -> tuple[Cell, ...]:
        return tuple(c for c in self.suppression.cells if c.is_published)

    def to_json(self) -> dict[str, Any]:
        payload = self.suppression.to_json(self.group_by)
        payload.update(
            {
                "metric": self.metric_key,
                "metricVersion": self.metric_version,
                "title": self.title,
                "unit": self.unit,
                "groupBy": list(self.group_by),
                "freshness": self.freshness.to_json(),
                "stale": self.stale,
                "lineage": self.lineage.to_json(),
                "rowsScanned": self.rows_scanned,
                "durationMs": round(self.duration_ms, 3),
                "warnings": list(self.warnings),
            }
        )
        return payload

    def render(self) -> str:
        lines = [f"{self.title} [{self.metric_key}@{self.metric_version}]"]
        if self.stale:
            lines.append("  ! STALE — data is older than this metric tolerates")
        for cell in self.suppression.cells:
            label = " / ".join(str(g) for g in cell.group) or "(all)"
            value = "suppressed" if cell.suppressed else f"{cell.value:,.4g}"
            lines.append(f"  {label:<40} {value:>14} {self.unit}")
        if not self.suppression.total_suppressed and self.suppression.total is not None:
            lines.append(f"  {'TOTAL':<40} {self.suppression.total:>14,.4g}")
        elif self.suppression.total_suppressed:
            lines.append(f"  {'TOTAL':<40} {'suppressed':>14}")
        if note := self.suppression.note():
            lines.append(f"  note: {note}")
        return "\n".join(lines)


class QueryExecutor:
    """Runs templates against the warehouse.

    Scans are tenant-scoped by construction: the warehouse's row iterator requires an
    organisation, so an unscoped scan is unwritable rather than merely discouraged.
    """

    def __init__(
        self,
        warehouse: Warehouse,
        metrics: MetricRegistry,
        templates: TemplateRegistry,
        *,
        max_scan_rows: int = 5_000_000,
    ) -> None:
        self._warehouse = warehouse
        self._metrics = metrics
        self._templates = templates
        self._max_scan_rows = max_scan_rows

    @property
    def warehouse(self) -> Warehouse:
        """Read-only access, for health and freshness reporting.

        Exposed deliberately narrow: callers read statistics and freshness, and there is no
        write path on the warehouse reachable from here at all.
        """
        return self._warehouse

    def execute(self, request: QueryRequest) -> MetricResult:
        started = time.perf_counter()
        template = self._templates.get(request.template_key)
        if template is None:
            raise QueryError(
                f"unknown template {request.template_key!r}; available: "
                f"{', '.join(self._templates.keys())}"
            )

        if not request.principal.holds(template.required_scope):
            raise QueryError(
                f"template {template.key!r} requires scope {template.required_scope}; the "
                "caller does not hold it"
            )

        metric = self._metrics.require(template.metric_key)

        if metric.disclosure.requires_elevated_scope and not request.principal.holds(
            ANALYTICS_ELEVATED
        ):
            raise QueryError(
                f"metric {metric.key!r} is patient-level and requires "
                f"{ANALYTICS_ELEVATED}, which is granted separately and audited as PHI access"
            )

        parameters = self._validate_parameters(template, request)
        group_by = self._validate_grouping(template, metric, request)

        rows, scanned = self._scan(metric, request.principal.organization_id, parameters)
        cells, total = self._aggregate(metric, rows, group_by)

        outcome = apply_disclosure_control(
            cells,
            policy=metric.disclosure,
            total=total,
            additive=metric.additivity.may_roll_up,
        )

        freshness = self._warehouse.freshness(metric.fact)
        stale = freshness.is_stale(request.at, dt.timedelta(hours=metric.freshness_tolerance_hours))
        warnings: list[str] = []
        if stale:
            # Returned marked stale rather than hidden. Hiding it produces an empty dashboard
            # with no explanation, which is worse than a labelled old number.
            warnings.append(
                f"{freshness.describe(request.at)}; this metric tolerates "
                f"{metric.freshness_tolerance_hours}h"
            )

        result = MetricResult(
            metric_key=metric.key,
            metric_version=metric.version,
            title=metric.title,
            unit=metric.unit,
            group_by=group_by,
            suppression=outcome,
            freshness=freshness,
            lineage=MetricLineage.of(metric, group_by),
            rows_scanned=scanned,
            duration_ms=(time.perf_counter() - started) * 1000,
            stale=stale,
            warnings=tuple(warnings),
        )
        _log.info(
            "query.executed",
            template=template.key,
            metric=metric.key,
            organization=request.principal.organization_id,
            rows=scanned,
            suppressed=outcome.suppressed_count,
            duration_ms=round(result.duration_ms, 2),
        )
        return result

    def _validate_parameters(
        self, template: QueryTemplate, request: QueryRequest
    ) -> dict[str, Any]:
        declared = {p.name for p in template.parameters}
        supplied = set(request.parameters) - {"group_by"}
        unknown = supplied - declared
        if unknown:
            raise QueryError(
                f"template {template.key!r} does not accept {sorted(unknown)}; accepted: "
                f"{sorted(declared)}"
            )

        coerced: dict[str, Any] = {}
        for parameter in template.parameters:
            if parameter.name not in request.parameters:
                if parameter.required:
                    raise QueryError(
                        f"template {template.key!r} requires parameter {parameter.name!r}"
                    )
                continue
            coerced[parameter.name] = parameter.coerce(request.parameters[parameter.name])

        start = coerced.get("from")
        end = coerced.get("to")
        if isinstance(start, dt.date) and isinstance(end, dt.date):
            if end < start:
                raise QueryError(f"'to' ({end}) is before 'from' ({start})")
            span = (end - start).days + 1
            if span > template.max_range_days:
                raise QueryError(
                    f"the requested range is {span} days; template {template.key!r} permits "
                    f"{template.max_range_days}. An unbounded range is both expensive and, on a "
                    "small population, a re-identification aid."
                )
        return coerced

    def _validate_grouping(
        self, template: QueryTemplate, metric: MetricDefinition, request: QueryRequest
    ) -> tuple[str, ...]:
        requested = request.group_by or metric.default_group_by
        if len(requested) > template.max_group_by:
            raise QueryError(
                f"template {template.key!r} permits {template.max_group_by} grouping "
                f"dimension(s); {len(requested)} were requested"
            )

        budget = QuasiIdentifierBudget(maximum=metric.disclosure.max_quasi_identifiers)
        fact = self._warehouse.schema.fact(metric.fact)
        # A quasi-identifier only identifies somebody if the rows are about somebody. Counting
        # the budget on an operational fact makes every such metric ungroupable by time, which
        # is not a privacy control — it is a broken dashboard.
        patient_level = fact is not None and fact.is_patient_level
        for dimension in requested:
            if dimension not in template.permitted_group_by and dimension not in (
                metric.default_group_by
            ):
                raise QueryError(
                    f"template {template.key!r} does not permit grouping by {dimension!r}; "
                    f"permitted: {sorted(template.permitted_group_by) or '(none)'}"
                )
            resolved = self._warehouse.schema.resolve_grouping(metric.fact, dimension)
            if resolved is None:
                raise QueryError(
                    f"{dimension!r} does not resolve to a groupable column on {metric.fact}"
                )
            _, column = resolved
            if patient_level and column.sensitivity is Sensitivity.QUASI_IDENTIFIER:
                budget.add(dimension)

        if budget.exceeded:
            raise QueryError(budget.refusal())
        return tuple(requested)

    def _scan(
        self, metric: MetricDefinition, organization_id: str, parameters: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], int]:
        start = parameters.get("from")
        end = parameters.get("to")
        low = _date_key(start) if isinstance(start, dt.date) else None
        high = _date_key(end) if isinstance(end, dt.date) else None

        kept: list[dict[str, Any]] = []
        scanned = 0
        for row in self._warehouse.rows(metric.fact, organization_id):
            scanned += 1
            if scanned > self._max_scan_rows:
                raise QueryError(
                    f"the query scanned more than {self._max_scan_rows:,} rows; narrow the "
                    "range or add a filter"
                )
            date_key = row.get("date_key")
            if low is not None and (date_key is None or date_key < low):
                continue
            if high is not None and (date_key is None or date_key > high):
                continue
            kept.append(row)
        return kept, scanned

    def _aggregate(
        self, metric: MetricDefinition, rows: list[dict[str, Any]], group_by: tuple[str, ...]
    ) -> tuple[list[Cell], float | int | None]:
        """Group and aggregate.

        A ratio is computed **per group** from its own numerator and denominator, never by
        averaging finer ratios. Averaging ratios weights every group equally regardless of its
        size, which is the classic wrong number.
        """
        resolved = [
            self._warehouse.schema.resolve_grouping(metric.fact, dimension)
            for dimension in group_by
        ]

        buckets: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
        for row in rows:
            key = tuple(
                self._group_value(metric.fact, row, dimension, entry)
                for dimension, entry in zip(group_by, resolved, strict=False)
            )
            buckets.setdefault(key, []).append(row)

        cells: list[Cell] = []
        for key, bucket in sorted(buckets.items(), key=lambda item: tuple(str(k) for k in item[0])):
            value, subjects = self._compute(metric, bucket)
            if value is None:
                continue
            cells.append(Cell(group=key, value=value, subjects=subjects))

        # The total is always recomputed over every row, never summed from the cells. For an
        # additive measure the two agree; for a rate or a distinct count they do not, and
        # summing would overcount. One code path means the wrong one cannot be taken.
        total, _ = self._compute(metric, rows)
        return cells, total

    def _group_value(
        self,
        fact_name: str,
        row: dict[str, Any],
        dimension: str,
        resolved: tuple[str, Any] | None,
    ) -> Any:
        """The value a row contributes to one grouping dimension.

        A bare name reads the fact column. A dotted name follows the fact's declared surrogate
        key into the dimension row — declared, so the join is never guessed from a naming
        convention.
        """
        if resolved is None:  # pragma: no cover - validated before aggregation
            return None
        _, column = resolved
        if "." not in dimension:
            return row.get(column.name)

        dimension_name = dimension.split(".", 1)[0]
        fact = self._warehouse.schema.fact(fact_name)
        if fact is None:  # pragma: no cover - the metric validated at registration
            return None
        key_column = next(
            (name for name, target in fact.dimension_keys.items() if target == dimension_name),
            None,
        )
        if key_column is None:  # pragma: no cover - validated at registration
            return None
        dimension_row = self._warehouse.dimension_row(dimension_name, row.get(key_column))
        # A fact pointing at a dimension row that was never loaded groups under "unknown"
        # rather than vanishing. Dropping it would make the cells fail to sum to the total and
        # silently understate every group.
        return "unknown" if dimension_row is None else dimension_row.get(column.name)

    def _compute(
        self, metric: MetricDefinition, rows: list[dict[str, Any]]
    ) -> tuple[float | int | None, int]:
        """Compute one measure over a bucket, returning (value, subject count)."""
        subject_column = metric.subject_column
        matching = [r for r in rows if all(f.matches(r) for f in metric.filters)]

        def subjects(candidate: list[dict[str, Any]]) -> int:
            if not subject_column:
                return len(candidate)
            # Distinct people, not rows. Ten observations on one patient is one subject, and
            # suppressing on the row count would publish a cell backed by a single person.
            return len({r.get(subject_column) for r in candidate if r.get(subject_column)})

        kind = metric.measure
        if kind is MeasureKind.COUNT:
            return len(matching), subjects(matching)
        if kind is MeasureKind.COUNT_DISTINCT:
            distinct = {r.get(metric.column) for r in matching if r.get(metric.column) is not None}
            return len(distinct), subjects(matching)
        if kind is MeasureKind.RATIO:
            denominator_rows = [
                r for r in rows if all(f.matches(r) for f in metric.denominator_filters)
            ]
            numerator_rows = [
                r for r in denominator_rows if all(f.matches(r) for f in metric.filters)
            ]
            if not denominator_rows:
                # No eligible population means no rate, not a rate of zero. Zero would put a
                # site at the bottom of a league table for having nobody eligible.
                return None, 0
            return (
                round(len(numerator_rows) / len(denominator_rows), 6),
                subjects(denominator_rows),
            )

        values = [
            float(r[metric.column])
            for r in matching
            if isinstance(r.get(metric.column), int | float)
        ]
        if not values:
            return None, subjects(matching)
        if kind is MeasureKind.SUM:
            return round(sum(values), 6), subjects(matching)
        if kind is MeasureKind.AVERAGE:
            return round(statistics.fmean(values), 6), subjects(matching)
        if kind is MeasureKind.MIN:
            return min(values), subjects(matching)
        if kind is MeasureKind.MAX:
            return max(values), subjects(matching)
        ordered = sorted(values)
        index = min(len(ordered) - 1, max(0, int(len(ordered) * metric.percentile) - 1))
        return ordered[index], subjects(matching)


def _date_key(when: dt.date) -> int:
    return when.year * 10000 + when.month * 100 + when.day
