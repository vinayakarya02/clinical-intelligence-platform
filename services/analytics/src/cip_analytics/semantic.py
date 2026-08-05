"""The semantic layer: metrics declared once, in data.

The characteristic failure of a BI layer is not a wrong query. It is two correct queries that
disagree — the clinical dashboard says 14.2%, the executive deck says 11.8%, and both are right
about different denominators. Nobody can tell which is wrong because neither is, and trust in
every number goes with it (docs/design/adr-0034-metric-is-a-definition.md).

So a metric is a declaration: key, version, source fact, measure, filters, an optional
denominator, a disclosure policy, and a freshness tolerance. Dashboards and reports reference a
metric **by key** and cannot express an aggregation of their own.

The registry refuses at load — every one of these is otherwise a wrong number nobody notices:

- an unknown fact or column, because a metric over a missing column returns zero and **zero is
  the most dangerous wrong answer**, since it reads as a real finding
- a ratio with no denominator
- a measure that needs a column and was not given one
- a filter on a column the fact does not have
- a metric with no disclosure policy
- two metrics sharing a key
"""

from __future__ import annotations

import datetime as dt
import pathlib
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import yaml

from cip_analytics.domain import (
    Additivity,
    DisclosurePolicy,
    MeasureKind,
    SchemaError,
)
from cip_analytics.warehouse import WarehouseSchema
from cip_core.logging import get_logger

__all__ = [
    "Filter",
    "FilterOperator",
    "MetricCategory",
    "MetricDefinition",
    "MetricRegistry",
    "load_metrics",
]

_log = get_logger(__name__)


class MetricCategory(StrEnum):
    """The four dashboard categories from the Phase 0 design.

    Declared on the metric rather than on the dashboard, so a metric's audience — and therefore
    the scope needed to read it — is a property of the metric itself.
    """

    CLINICAL = "clinical"
    OPERATIONAL = "operational"
    GOVERNANCE = "governance"
    USAGE = "usage"

    @property
    def default_scope(self) -> str:
        """Governance metrics summarise who accessed what and need their own scope; the rest
        are readable by any analyst."""
        return "analytics:governance" if self is MetricCategory.GOVERNANCE else "analytics:read"


class FilterOperator(StrEnum):
    """The comparisons a metric filter may use.

    A closed set. This is the same refusal the rules engine and the mapping engine make: a
    declaration file must not be a program, so there is no expression syntax here to evaluate.
    """

    EQUALS = "eq"
    NOT_EQUALS = "ne"
    IN = "in"
    NOT_IN = "not_in"
    GREATER_THAN = "gt"
    GREATER_OR_EQUAL = "gte"
    LESS_THAN = "lt"
    LESS_OR_EQUAL = "lte"
    IS_TRUE = "is_true"
    IS_FALSE = "is_false"
    IS_NULL = "is_null"
    NOT_NULL = "not_null"

    @property
    def takes_list(self) -> bool:
        return self in (FilterOperator.IN, FilterOperator.NOT_IN)

    @property
    def takes_value(self) -> bool:
        return self not in (
            FilterOperator.IS_TRUE,
            FilterOperator.IS_FALSE,
            FilterOperator.IS_NULL,
            FilterOperator.NOT_NULL,
        )


@dataclass(frozen=True, slots=True)
class Filter:
    """One condition on a metric's source rows."""

    column: str
    operator: FilterOperator
    value: Any = None

    def __post_init__(self) -> None:
        if self.operator.takes_value and self.value is None:
            raise SchemaError(
                f"filter {self.column} {self.operator.value} needs a value; use is_null or "
                "not_null to test for absence"
            )
        if self.operator.takes_list and not isinstance(self.value, list | tuple | set):
            raise SchemaError(f"filter {self.column} {self.operator.value} needs a list")

    def matches(self, row: dict[str, Any]) -> bool:
        actual = row.get(self.column)
        operator = self.operator
        if operator is FilterOperator.IS_NULL:
            return actual is None
        if operator is FilterOperator.NOT_NULL:
            return actual is not None
        if operator is FilterOperator.IS_TRUE:
            return actual is True
        if operator is FilterOperator.IS_FALSE:
            return actual is False
        if actual is None:
            # A null never satisfies a comparison. SQL agrees, and the alternative — treating
            # null as zero or as the empty string — silently pulls unmeasured rows into a
            # denominator.
            return False
        if operator is FilterOperator.EQUALS:
            return bool(actual == self.value)
        if operator is FilterOperator.NOT_EQUALS:
            return bool(actual != self.value)
        if operator is FilterOperator.IN:
            return actual in self.value
        if operator is FilterOperator.NOT_IN:
            return actual not in self.value
        try:
            if operator is FilterOperator.GREATER_THAN:
                return bool(actual > self.value)
            if operator is FilterOperator.GREATER_OR_EQUAL:
                return bool(actual >= self.value)
            if operator is FilterOperator.LESS_THAN:
                return bool(actual < self.value)
            return bool(actual <= self.value)
        except TypeError:
            # Comparing a string to a number is a declaration error that has already passed
            # load-time checks because the column's type allows both in principle. Treat the
            # row as non-matching and let the count look wrong loudly rather than crash a
            # dashboard.
            _log.warning(
                "semantic.filter_type_mismatch",
                column=self.column,
                operator=operator.value,
                actual_type=type(actual).__name__,
            )
            return False

    def render(self) -> str:
        if not self.operator.takes_value:
            return f"{self.column} {self.operator.value}"
        return f"{self.column} {self.operator.value} {self.value!r}"


@dataclass(frozen=True, slots=True)
class MetricDefinition:
    """One metric, declared."""

    key: str
    version: str
    title: str
    category: MetricCategory
    fact: str
    measure: MeasureKind
    disclosure: DisclosurePolicy
    column: str = ""
    subject_column: str = ""
    """The column counted for disclosure control — usually the pseudonymous patient key. A
    metric over patients whose subject count is its row count will suppress the wrong things:
    ten observations on one patient is one subject, not ten."""
    filters: tuple[Filter, ...] = ()
    denominator_filters: tuple[Filter, ...] = ()
    """For a ratio: the rows forming the denominator. The numerator is those rows that also
    satisfy ``filters``, so a numerator can never exceed its denominator by construction."""
    default_group_by: tuple[str, ...] = ()
    allowed_group_by: tuple[str, ...] = ()
    freshness_tolerance_hours: int = 48
    percentile: float = 0.95
    description: str = ""
    effective_from: dt.date | None = None
    unit: str = ""

    def __post_init__(self) -> None:
        if not self.key.strip():
            raise SchemaError("metric key must not be empty")
        if not self.version.strip():
            raise SchemaError(
                f"metric {self.key!r} has no version; a definition that changes silently makes "
                "a historical chart un-reproducible"
            )
        if self.measure.needs_column and not self.column:
            raise SchemaError(
                f"metric {self.key!r} uses {self.measure.value}, which aggregates a column, but "
                "declares none"
            )
        if self.measure.needs_denominator and not self.denominator_filters:
            raise SchemaError(
                f"metric {self.key!r} is a ratio with no denominator filters. A ratio whose "
                "denominator is every row in the fact is almost never what was meant."
            )
        if not 0.0 < self.percentile < 1.0:
            raise SchemaError(f"metric {self.key!r} has percentile {self.percentile} outside (0,1)")

    @property
    def additivity(self) -> Additivity:
        return self.measure.additivity

    @property
    def full_key(self) -> str:
        return f"{self.key}@{self.version}"

    def is_effective(self, on: dt.date) -> bool:
        return self.effective_from is None or on >= self.effective_from

    def permits_group_by(self, dimension: str) -> bool:
        """Whether this metric may be grouped by a dimension.

        An empty ``allowed_group_by`` permits nothing beyond the default. Deliberately
        restrictive: the permissive reading — empty means everything — is how a metric ends up
        groupable by every quasi-identifier the fact has.
        """
        return dimension in self.allowed_group_by or dimension in self.default_group_by

    def to_json(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "version": self.version,
            "title": self.title,
            "category": str(self.category),
            "fact": self.fact,
            "measure": str(self.measure),
            "additivity": str(self.additivity),
            "unit": self.unit,
            "allowedGroupBy": list(self.allowed_group_by),
            "minimumCellSize": self.disclosure.minimum_cell_size,
            "requiresElevatedScope": self.disclosure.requires_elevated_scope,
            "freshnessToleranceHours": self.freshness_tolerance_hours,
        }


def _parse_filters(raw: list[dict[str, Any]] | None, *, metric_key: str) -> tuple[Filter, ...]:
    filters: list[Filter] = []
    for entry in raw or []:
        unknown = set(entry) - {"column", "op", "value"}
        if unknown:
            raise SchemaError(
                f"metric {metric_key!r} filter has unknown keys {sorted(unknown)}; a misspelled "
                "key is silently ignored, so it is refused"
            )
        try:
            operator = FilterOperator(str(entry.get("op", "")))
        except ValueError as exc:
            raise SchemaError(
                f"metric {metric_key!r} uses unknown filter operator {entry.get('op')!r}; "
                f"available: {', '.join(sorted(o.value for o in FilterOperator))}"
            ) from exc
        filters.append(
            Filter(column=str(entry["column"]), operator=operator, value=entry.get("value"))
        )
    return tuple(filters)


class MetricRegistry:
    """Every metric this deployment can compute."""

    def __init__(self, schema: WarehouseSchema) -> None:
        self._schema = schema
        self._metrics: dict[str, MetricDefinition] = {}

    def register(self, metric: MetricDefinition) -> None:
        """Add a metric, validating it against the warehouse schema."""
        if metric.key in self._metrics:
            raise SchemaError(
                f"metric {metric.key!r} is already registered; two definitions of one key is "
                "exactly the disagreement this layer exists to prevent"
            )
        fact = self._schema.fact(metric.fact)
        if fact is None:
            raise SchemaError(f"metric {metric.key!r} reads unknown fact {metric.fact!r}")

        if metric.column and fact.column(metric.column) is None:
            raise SchemaError(
                f"metric {metric.key!r} aggregates {metric.column!r}, which is not a column of "
                f"{metric.fact}. A metric over a missing column returns zero, and zero reads as "
                "a real finding."
            )
        if metric.column:
            column = fact.column(metric.column)
            assert column is not None
            if (
                metric.measure
                in (
                    MeasureKind.SUM,
                    MeasureKind.AVERAGE,
                    MeasureKind.MIN,
                    MeasureKind.MAX,
                    MeasureKind.PERCENTILE,
                )
                and not column.is_measurable
            ):
                raise SchemaError(
                    f"metric {metric.key!r} applies {metric.measure.value} to non-numeric "
                    f"column {metric.column!r}"
                )
        if fact.is_patient_level and not metric.subject_column:
            raise SchemaError(
                f"metric {metric.key!r} reads patient-level fact {metric.fact!r} but declares no "
                "subject_column, so disclosure control would count ROWS rather than PEOPLE. "
                "Twenty observations from three patients would pass a threshold of eleven and "
                "publish a cell backed by three people, which is exactly what the threshold "
                "exists to prevent."
            )
        if metric.subject_column and fact.column(metric.subject_column) is None:
            raise SchemaError(
                f"metric {metric.key!r} counts subjects on {metric.subject_column!r}, which is "
                f"not a column of {metric.fact}"
            )

        for condition in (*metric.filters, *metric.denominator_filters):
            if fact.column(condition.column) is None:
                raise SchemaError(
                    f"metric {metric.key!r} filters on {condition.column!r}, which is not a "
                    f"column of {metric.fact}"
                )

        for dimension in (*metric.default_group_by, *metric.allowed_group_by):
            if self._schema.resolve_grouping(metric.fact, dimension) is None:
                raise SchemaError(
                    f"metric {metric.key!r} permits grouping by {dimension!r}, which does not "
                    f"resolve on {metric.fact}. Grouping by a key or an unjoined dimension is "
                    "refused."
                )

        self._metrics[metric.key] = metric

    def get(self, key: str) -> MetricDefinition | None:
        return self._metrics.get(key)

    def require(self, key: str) -> MetricDefinition:
        metric = self._metrics.get(key)
        if metric is None:
            raise SchemaError(
                f"unknown metric {key!r}; available: {', '.join(sorted(self._metrics))}"
            )
        return metric

    def by_category(self, category: MetricCategory) -> tuple[MetricDefinition, ...]:
        return tuple(
            sorted(
                (m for m in self._metrics.values() if m.category is category),
                key=lambda m: m.key,
            )
        )

    def keys(self) -> tuple[str, ...]:
        return tuple(sorted(self._metrics))

    def count(self) -> int:
        return len(self._metrics)

    def catalogue(self) -> list[dict[str, Any]]:
        """The published list of what can be asked.

        A governance artifact as much as a developer convenience: "what can be computed from
        this data" is a question a compliance officer needs answered, and a registry makes it
        answerable by enumeration.
        """
        return [m.to_json() for m in sorted(self._metrics.values(), key=lambda m: m.key)]


def load_metrics(path: pathlib.Path | str, schema: WarehouseSchema) -> MetricRegistry:
    """Load and validate a metric definition file."""
    location = pathlib.Path(path)
    try:
        raw = yaml.safe_load(location.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise SchemaError(f"{location.name} is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict) or "metrics" not in raw:
        raise SchemaError(f"{location.name} must contain a top-level 'metrics' list")

    registry = MetricRegistry(schema)
    for entry in raw["metrics"]:
        key = str(entry.get("key", ""))
        unknown = set(entry) - {
            "key",
            "version",
            "title",
            "category",
            "fact",
            "measure",
            "column",
            "subject_column",
            "filters",
            "denominator_filters",
            "default_group_by",
            "allowed_group_by",
            "disclosure",
            "freshness_tolerance_hours",
            "percentile",
            "description",
            "effective_from",
            "unit",
        }
        if unknown:
            raise SchemaError(f"metric {key!r} has unknown keys {sorted(unknown)}")

        disclosure_raw = entry.get("disclosure")
        if disclosure_raw is None:
            raise SchemaError(
                f"metric {key!r} declares no disclosure policy. A metric nobody classified is "
                "one that gets published at whatever the default is."
            )
        disclosure = DisclosurePolicy(
            minimum_cell_size=int(disclosure_raw.get("minimum_cell_size", 11)),
            suppress_totals_when_needed=bool(
                disclosure_raw.get("suppress_totals_when_needed", True)
            ),
            max_quasi_identifiers=int(disclosure_raw.get("max_quasi_identifiers", 2)),
            requires_elevated_scope=bool(disclosure_raw.get("requires_elevated_scope", False)),
        )

        try:
            category = MetricCategory(str(entry.get("category", "")))
            measure = MeasureKind(str(entry.get("measure", "")))
        except ValueError as exc:
            raise SchemaError(f"metric {key!r}: {exc}") from exc

        effective = entry.get("effective_from")
        registry.register(
            MetricDefinition(
                key=key,
                version=str(entry.get("version", "")),
                title=str(entry.get("title", key)),
                category=category,
                fact=str(entry.get("fact", "")),
                measure=measure,
                disclosure=disclosure,
                column=str(entry.get("column", "")),
                subject_column=str(entry.get("subject_column", "")),
                filters=_parse_filters(entry.get("filters"), metric_key=key),
                denominator_filters=_parse_filters(
                    entry.get("denominator_filters"), metric_key=key
                ),
                default_group_by=tuple(entry.get("default_group_by") or []),
                allowed_group_by=tuple(entry.get("allowed_group_by") or []),
                freshness_tolerance_hours=int(entry.get("freshness_tolerance_hours", 48)),
                percentile=float(entry.get("percentile", 0.95)),
                description=str(entry.get("description", "")),
                effective_from=dt.date.fromisoformat(effective) if effective else None,
                unit=str(entry.get("unit", "")),
            )
        )

    _log.info("semantic.loaded", metrics=registry.count(), file=location.name)
    return registry


@dataclass(frozen=True, slots=True)
class MetricLineage:
    """Where a metric's number came from.

    Returned with every result so a chart can be reconstructed: the definition, its version, the
    fact it read, and the filters applied. "Why is this number different from last quarter's
    deck" is otherwise unanswerable.
    """

    metric_key: str
    metric_version: str
    fact: str
    measure: str
    filters: tuple[str, ...] = ()
    group_by: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "metric": f"{self.metric_key}@{self.metric_version}",
            "fact": self.fact,
            "measure": self.measure,
            "filters": list(self.filters),
            "groupBy": list(self.group_by),
        }

    @classmethod
    def of(cls, metric: MetricDefinition, group_by: tuple[str, ...] = ()) -> MetricLineage:
        return cls(
            metric_key=metric.key,
            metric_version=metric.version,
            fact=metric.fact,
            measure=metric.measure.value,
            filters=tuple(f.render() for f in metric.filters),
            group_by=group_by,
        )
