"""Domain types for the analytics warehouse.

Imports nothing else from this package — the base of the dependency order, enforced by a test.

Three design points carry the phase.

:class:`Additivity` is declared per measure and enforced. A count is additive across every
dimension; a rate is not additive at all and must be recomputed from numerator and denominator
at whatever level it is grouped. **Summing a rate is the most common wrong number in a BI
layer**, and a type that refuses is better than a convention that asks.

:class:`Freshness` travels with every result. A dashboard that does not say how stale it is
invites a decision on last week's numbers, and the person making it has no way to know.

:class:`DisclosurePolicy` is mandatory on every metric. A metric nobody classified is one that
gets published at whatever the default is.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

__all__ = [
    "Additivity",
    "AnalyticsError",
    "ColumnType",
    "DisclosurePolicy",
    "Freshness",
    "Grain",
    "MeasureKind",
    "QueryError",
    "SchemaError",
    "Sensitivity",
    "SuppressionError",
]


class AnalyticsError(Exception):
    """Base for every failure in this service."""


class SchemaError(AnalyticsError):
    """A schema, metric, or template declaration is not valid."""


class QueryError(AnalyticsError):
    """A query could not be run as asked."""


class SuppressionError(AnalyticsError):
    """A result could not be made safe to publish."""


class ColumnType(StrEnum):
    """The types a warehouse column may hold.

    Deliberately small. A warehouse column that can hold anything cannot be validated at load,
    and a fact table that accepts anything is one where a unit change goes unnoticed for a year.
    """

    KEY = "key"
    """A surrogate or pseudonymous key. Never displayed, never aggregated."""
    TEXT = "text"
    DATE = "date"
    INTEGER = "integer"
    DECIMAL = "decimal"
    BOOLEAN = "boolean"

    @property
    def is_measurable(self) -> bool:
        return self in (ColumnType.INTEGER, ColumnType.DECIMAL)

    @property
    def is_groupable(self) -> bool:
        """Whether a query may group by this column.

        Keys are excluded: grouping by a pseudonymous patient key produces one row per patient,
        which is a patient-level extract wearing an aggregate's clothes.
        """
        return self in (ColumnType.TEXT, ColumnType.DATE, ColumnType.BOOLEAN, ColumnType.INTEGER)


class Sensitivity(StrEnum):
    """How closely a column identifies a person.

    Drives disclosure control rather than access control: two ``QUASI_IDENTIFIER`` columns in one
    grouping are how a de-identified table identifies somebody, so the count of them in a query
    is bounded regardless of who is asking.
    """

    NON_IDENTIFYING = "non_identifying"
    """Organisation, source system, document type. Combining these identifies nobody."""
    QUASI_IDENTIFIER = "quasi_identifier"
    """Postal prefix, birth year, sex, admission month. Individually harmless, jointly not."""
    PSEUDONYM = "pseudonym"
    """A salted patient or actor key. Present so facts can be counted distinctly; never
    groupable and never returned."""

    @property
    def counts_toward_quasi_identifier_budget(self) -> bool:
        return self is Sensitivity.QUASI_IDENTIFIER


class Grain(StrEnum):
    """What one row of a fact table represents.

    Declared per fact and checked. A fact table with an undeclared grain makes every ``COUNT``
    ambiguous, and the ambiguity is invisible until two dashboards disagree.
    """

    ENCOUNTER = "encounter"
    OBSERVATION = "observation"
    DOCUMENT = "document"
    QUERY = "query"
    ANSWER = "answer"
    ACCESS_EVENT = "access_event"
    JOB_RUN = "job_run"
    PATIENT_DAY = "patient_day"


class MeasureKind(StrEnum):
    """How a measure is computed."""

    COUNT = "count"
    COUNT_DISTINCT = "count_distinct"
    SUM = "sum"
    AVERAGE = "average"
    MIN = "min"
    MAX = "max"
    RATIO = "ratio"
    PERCENTILE = "percentile"

    @property
    def additivity(self) -> Additivity:
        """How this measure behaves when rolled up."""
        if self in (MeasureKind.COUNT, MeasureKind.SUM):
            return Additivity.ADDITIVE
        if self is MeasureKind.COUNT_DISTINCT:
            # Distinct counts do not add: the same patient in two months is one patient in the
            # year, and summing the months overcounts. It must be recomputed at the grouping
            # level asked for.
            return Additivity.NON_ADDITIVE
        if self in (MeasureKind.AVERAGE, MeasureKind.RATIO, MeasureKind.PERCENTILE):
            return Additivity.NON_ADDITIVE
        return Additivity.SEMI_ADDITIVE

    @property
    def needs_denominator(self) -> bool:
        return self is MeasureKind.RATIO

    @property
    def needs_column(self) -> bool:
        """Whether this measure aggregates a specific column.

        ``COUNT`` and ``RATIO`` do not — both count rows, the ratio counting two sets of them
        selected by filters. Requiring a column for either invites a metric that counts non-null
        values of an arbitrary column and calls it a row count.
        """
        return self not in (MeasureKind.COUNT, MeasureKind.RATIO)


class Additivity(StrEnum):
    """Whether a measure may be summed across dimensions."""

    ADDITIVE = "additive"
    SEMI_ADDITIVE = "semi_additive"
    """Additive across some dimensions but not time — a stock balance, for instance."""
    NON_ADDITIVE = "non_additive"

    @property
    def may_roll_up(self) -> bool:
        return self is Additivity.ADDITIVE


@dataclass(frozen=True, slots=True)
class DisclosurePolicy:
    """How a metric's results must be protected before publication.

    Mandatory on every metric. There is deliberately no default: a default means an
    unclassified metric is published at whatever somebody chose once, and nobody revisits it.
    """

    minimum_cell_size: int = 11
    """Cells with fewer subjects are suppressed. Eleven is a common default in US health
    statistics; other jurisdictions and datasets use other values, so it is configuration."""
    suppress_totals_when_needed: bool = True
    """Whether a total may be withheld to protect a suppressed cell. Turning this off makes
    suppression defeatable by subtraction (docs/design/adr-0036-complementary-suppression.md)."""
    max_quasi_identifiers: int = 2
    """How many quasi-identifying dimensions may appear in one grouping. Three is enough to
    single out a person in a small population."""
    requires_elevated_scope: bool = False
    """Whether this metric is patient-level and needs the audited elevated path."""

    def __post_init__(self) -> None:
        if self.minimum_cell_size < 1:
            raise SchemaError("minimum_cell_size must be at least 1")
        if self.max_quasi_identifiers < 0:
            raise SchemaError("max_quasi_identifiers must not be negative")

    @property
    def is_public_safe(self) -> bool:
        """Whether results under this policy could be published without further review."""
        return (
            not self.requires_elevated_scope
            and self.minimum_cell_size >= 11
            and self.suppress_totals_when_needed
        )


@dataclass(frozen=True, slots=True)
class Freshness:
    """How current the data behind a result is.

    Carried on every result. ``as_of`` is the watermark the ETL reached, **not** the time the
    query ran — a query run now over data loaded yesterday is a yesterday answer, and reporting
    the query time would say otherwise.
    """

    as_of: dt.datetime | None
    load_id: str = ""
    ruleset_version: str = ""
    """The de-identification ruleset the loaded rows were produced under. A warehouse holds the
    output of a ruleset version, so a result is only interpretable alongside it."""

    def age(self, now: dt.datetime) -> dt.timedelta | None:
        return None if self.as_of is None else now - self.as_of

    def is_stale(self, now: dt.datetime, tolerance: dt.timedelta) -> bool:
        """Whether the data is older than a metric tolerates.

        Unknown freshness counts as stale. A warehouse that cannot say when it was loaded should
        not be treated as current.
        """
        age = self.age(now)
        return age is None or age > tolerance

    def describe(self, now: dt.datetime) -> str:
        if self.as_of is None:
            return "freshness unknown"
        age = self.age(now)
        assert age is not None
        hours = age.total_seconds() / 3600
        return f"data as of {self.as_of.isoformat(timespec='minutes')} ({hours:.1f}h old)"

    def to_json(self) -> dict[str, Any]:
        return {
            "asOf": self.as_of.isoformat() if self.as_of else None,
            "loadId": self.load_id,
            "rulesetVersion": self.ruleset_version,
        }


@dataclass(frozen=True, slots=True)
class Scope:
    """An RBAC scope required to run something.

    A plain value rather than a string so a template's requirement and a caller's grant compare
    by type. Comparing bare strings is how ``analytics:read`` and ``analytics.read`` end up both
    in use and silently never matching.
    """

    name: str

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise SchemaError("Scope.name must not be empty")

    def __str__(self) -> str:
        return self.name


#: Scopes this service recognises. Enumerated so a typo in a template fails at load.
ANALYTICS_READ = Scope("analytics:read")
ANALYTICS_GOVERNANCE = Scope("analytics:governance")
ANALYTICS_ELEVATED = Scope("analytics:elevated")
KNOWN_SCOPES: frozenset[str] = frozenset(
    {ANALYTICS_READ.name, ANALYTICS_GOVERNANCE.name, ANALYTICS_ELEVATED.name}
)


@dataclass(frozen=True, slots=True)
class AnalyticsPrincipal:
    """Who is asking.

    Threaded explicitly rather than held in ambient state, for the same reason as every other
    context object in this platform: one that can be forgotten will be, and the resulting query
    runs unscoped.
    """

    principal_id: str
    organization_id: str
    scopes: frozenset[str] = field(default_factory=frozenset)
    roles: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if not self.principal_id.strip():
            raise SchemaError("AnalyticsPrincipal.principal_id must not be empty")
        if not self.organization_id.strip():
            raise SchemaError(
                "AnalyticsPrincipal.organization_id must not be empty; an unscoped analytics "
                "caller would read every tenant's warehouse"
            )

    def holds(self, scope: Scope) -> bool:
        return scope.name in self.scopes
