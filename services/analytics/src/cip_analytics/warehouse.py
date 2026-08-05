"""The dimensional warehouse: star schema and a typed, tenant-scoped store.

Conformed dimensions shared by every fact, so two facts grouped by the same dimension are
grouped the same way. That is what "conformed" buys, and it is why a dimensional model is used
here rather than a pile of purpose-built aggregate tables that each define "month" slightly
differently.

Two rules are enforced rather than documented, because both failures are invisible until two
dashboards disagree:

**One declared grain per fact.** ``fact_observation`` is one row per observation — not per
patient, not per encounter. A table mixing grains makes every count ambiguous.

**Declared additivity per measure.** A rate must be recomputed at the grouping level asked for,
never summed from finer rows.

The store is in-memory and columnar. It is not a database; it is the **contract** a database
must satisfy, expressed so it can be tested — the same approach taken for the vector store in
Phase 2 and the FHIR repository in Phase 6.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from cip_analytics.domain import (
    ColumnType,
    Freshness,
    Grain,
    SchemaError,
    Sensitivity,
)
from cip_core.logging import get_logger

__all__ = [
    "Column",
    "DimensionTable",
    "FactTable",
    "Warehouse",
    "WarehouseSchema",
    "default_schema",
]

_log = get_logger(__name__)

#: Every fact carries these. Declared once rather than repeated per fact, because a fact that
#: forgot its tenant column is one that leaks across organisations.
_MANDATORY_FACT_COLUMNS = ("organization_id", "date_key", "load_id")


@dataclass(frozen=True, slots=True)
class Column:
    """One warehouse column."""

    name: str
    type: ColumnType
    sensitivity: Sensitivity = Sensitivity.NON_IDENTIFYING
    description: str = ""
    nullable: bool = True

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise SchemaError("Column.name must not be empty")
        if self.type is ColumnType.KEY and self.sensitivity is Sensitivity.NON_IDENTIFYING:
            # A key that claims to identify nobody is a key somebody will group by. Keys are
            # pseudonyms or they are surrogate ids, and either way they are not "non-identifying".
            raise SchemaError(
                f"column {self.name!r} is a key but declares NON_IDENTIFYING sensitivity; a key "
                "is a pseudonym or a surrogate, and neither is safe to group by"
            )

    @property
    def is_groupable(self) -> bool:
        return self.type.is_groupable and self.sensitivity is not Sensitivity.PSEUDONYM

    @property
    def is_measurable(self) -> bool:
        return self.type.is_measurable


@dataclass(frozen=True, slots=True)
class DimensionTable:
    """A conformed dimension.

    ``key`` is the surrogate joined from facts. Natural keys change — an organisation is
    renamed, a code is retired — and a fact joined on a natural key silently re-parents itself
    when that happens.
    """

    name: str
    key: str
    columns: tuple[Column, ...]
    description: str = ""

    def __post_init__(self) -> None:
        names = [c.name for c in self.columns]
        if self.key not in names:
            raise SchemaError(f"dimension {self.name!r} has no column for its key {self.key!r}")
        if len(names) != len(set(names)):
            raise SchemaError(f"dimension {self.name!r} has duplicate column names")

    def column(self, name: str) -> Column | None:
        return next((c for c in self.columns if c.name == name), None)

    def groupable_columns(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.columns if c.is_groupable and c.name != self.key)


@dataclass(frozen=True, slots=True)
class FactTable:
    """A fact table at exactly one declared grain."""

    name: str
    grain: Grain
    columns: tuple[Column, ...]
    #: ``column name -> dimension name``. Declared so a query can resolve a grouping request to
    #: a join without guessing, and so an unjoinable grouping fails at load rather than at run.
    dimension_keys: dict[str, str] = field(default_factory=dict)
    description: str = ""

    def __post_init__(self) -> None:
        names = {c.name for c in self.columns}
        if len(names) != len(self.columns):
            raise SchemaError(f"fact {self.name!r} has duplicate column names")
        for required in _MANDATORY_FACT_COLUMNS:
            if required not in names:
                raise SchemaError(
                    f"fact {self.name!r} is missing the mandatory column {required!r}. Without "
                    "it the fact cannot be tenant-scoped, dated, or traced to a load."
                )
        for column_name in self.dimension_keys:
            if column_name not in names:
                raise SchemaError(
                    f"fact {self.name!r} declares a dimension key on {column_name!r}, which is "
                    "not one of its columns"
                )

    def column(self, name: str) -> Column | None:
        return next((c for c in self.columns if c.name == name), None)

    def measurable_columns(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.columns if c.is_measurable)

    def groupable_columns(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.columns if c.is_groupable)

    @property
    def is_patient_level(self) -> bool:
        """Whether rows of this fact are about identifiable people.

        The distinction that makes conformed dimensions workable. ``dim_date.month`` is a
        quasi-identifier on an observation — admission month narrows a person down — and is not
        one at all on a job run or a disclosure decision, which are about systems and roles.

        Sensitivity is therefore declared on the column *and* interpreted in the context of the
        fact. Without this, either every operational metric becomes ungroupable by time, or
        every patient metric becomes groupable by a quasi-identifier — and the first is what
        happened when this was first written.
        """
        return any(
            c.name in ("patient_key", "cohort_key") and c.sensitivity is Sensitivity.PSEUDONYM
            for c in self.columns
        )

    def quasi_identifiers(self) -> tuple[str, ...]:
        return tuple(
            c.name for c in self.columns if c.sensitivity.counts_toward_quasi_identifier_budget
        )


@dataclass(frozen=True, slots=True)
class WarehouseSchema:
    """The whole model."""

    facts: tuple[FactTable, ...]
    dimensions: tuple[DimensionTable, ...]
    version: str = "1.0.0"

    def __post_init__(self) -> None:
        dimension_names = {d.name for d in self.dimensions}
        for fact in self.facts:
            for column_name, dimension_name in fact.dimension_keys.items():
                if dimension_name not in dimension_names:
                    raise SchemaError(
                        f"fact {fact.name!r} joins {column_name!r} to unknown dimension "
                        f"{dimension_name!r}"
                    )

    def fact(self, name: str) -> FactTable | None:
        return next((f for f in self.facts if f.name == name), None)

    def dimension(self, name: str) -> DimensionTable | None:
        return next((d for d in self.dimensions if d.name == name), None)

    def resolve_grouping(self, fact_name: str, expression: str) -> tuple[str, Column] | None:
        """Resolve a grouping expression to (source table, column).

        Accepts a bare fact column (``source_system``) or a dotted dimension reference
        (``dim_date.month``). Returns ``None`` when it does not resolve — the caller decides
        whether that is a refusal, because a query template and an ad hoc call want different
        error text.
        """
        fact = self.fact(fact_name)
        if fact is None:
            return None
        if "." in expression:
            dimension_name, _, column_name = expression.partition(".")
            if dimension_name not in set(fact.dimension_keys.values()):
                return None
            dimension = self.dimension(dimension_name)
            if dimension is None:
                return None
            column = dimension.column(column_name)
            return (dimension_name, column) if column and column.is_groupable else None
        column = fact.column(expression)
        return (fact_name, column) if column and column.is_groupable else None


def _c(
    name: str,
    type_: ColumnType,
    sensitivity: Sensitivity = Sensitivity.NON_IDENTIFYING,
    description: str = "",
) -> Column:
    return Column(name=name, type=type_, sensitivity=sensitivity, description=description)


def default_schema() -> WarehouseSchema:
    """The shipped model.

    Facts cover the platform's own lifecycle as well as clinical events, because the operational
    and usage dashboard categories are about the platform and the clinical categories are about
    patients — and both need a warehouse, not a live query against the systems they measure.
    """
    dim_date = DimensionTable(
        name="dim_date",
        key="date_key",
        description="Conformed calendar. Day is the finest grain the warehouse holds.",
        columns=(
            _c("date_key", ColumnType.KEY, Sensitivity.PSEUDONYM, "yyyymmdd surrogate"),
            _c("date", ColumnType.DATE, Sensitivity.QUASI_IDENTIFIER, "The day itself"),
            _c("month", ColumnType.TEXT, Sensitivity.QUASI_IDENTIFIER, "yyyy-mm"),
            _c("quarter", ColumnType.TEXT, Sensitivity.NON_IDENTIFYING, "yyyy-Qn"),
            _c("year", ColumnType.INTEGER, Sensitivity.NON_IDENTIFYING),
            _c("day_of_week", ColumnType.TEXT, Sensitivity.NON_IDENTIFYING),
            _c("is_weekend", ColumnType.BOOLEAN, Sensitivity.NON_IDENTIFYING),
        ),
    )
    dim_organization = DimensionTable(
        name="dim_organization",
        key="organization_key",
        description="Organisations and facilities, conformed with the interop directory.",
        columns=(
            _c("organization_key", ColumnType.KEY, Sensitivity.PSEUDONYM),
            _c("organization_id", ColumnType.TEXT, Sensitivity.NON_IDENTIFYING),
            _c("organization_name", ColumnType.TEXT, Sensitivity.NON_IDENTIFYING),
            _c("organization_kind", ColumnType.TEXT, Sensitivity.NON_IDENTIFYING),
            _c("region", ColumnType.TEXT, Sensitivity.NON_IDENTIFYING),
        ),
    )
    dim_cohort = DimensionTable(
        name="dim_cohort",
        key="cohort_key",
        description="De-identified patient attributes. Every column here is a quasi-identifier.",
        columns=(
            _c("cohort_key", ColumnType.KEY, Sensitivity.PSEUDONYM),
            _c(
                "age_band", ColumnType.TEXT, Sensitivity.QUASI_IDENTIFIER, "5-year band, 90+ capped"
            ),
            _c("sex", ColumnType.TEXT, Sensitivity.QUASI_IDENTIFIER),
            _c("postal_prefix", ColumnType.TEXT, Sensitivity.QUASI_IDENTIFIER, "3 digits at most"),
            _c("risk_band", ColumnType.TEXT, Sensitivity.NON_IDENTIFYING),
        ),
    )
    dim_code = DimensionTable(
        name="dim_code",
        key="code_key",
        description="Clinical codes with their system.",
        columns=(
            _c("code_key", ColumnType.KEY, Sensitivity.PSEUDONYM),
            _c("code", ColumnType.TEXT, Sensitivity.NON_IDENTIFYING),
            _c("code_system", ColumnType.TEXT, Sensitivity.NON_IDENTIFYING),
            _c("display", ColumnType.TEXT, Sensitivity.NON_IDENTIFYING),
            _c("category", ColumnType.TEXT, Sensitivity.NON_IDENTIFYING),
        ),
    )
    dim_actor_role = DimensionTable(
        name="dim_actor_role",
        key="role_key",
        description="Roles, never individual actors. Governance reporting is about roles.",
        columns=(
            _c("role_key", ColumnType.KEY, Sensitivity.PSEUDONYM),
            _c("role", ColumnType.TEXT, Sensitivity.NON_IDENTIFYING),
            _c("is_service_account", ColumnType.BOOLEAN, Sensitivity.NON_IDENTIFYING),
        ),
    )
    dim_source_system = DimensionTable(
        name="dim_source_system",
        key="source_key",
        description="Sending systems and channels.",
        columns=(
            _c("source_key", ColumnType.KEY, Sensitivity.PSEUDONYM),
            _c("source_system", ColumnType.TEXT, Sensitivity.NON_IDENTIFYING),
            _c("channel", ColumnType.TEXT, Sensitivity.NON_IDENTIFYING),
            _c("interface_kind", ColumnType.TEXT, Sensitivity.NON_IDENTIFYING),
        ),
    )

    common = (
        _c("organization_id", ColumnType.TEXT, Sensitivity.NON_IDENTIFYING),
        _c("date_key", ColumnType.INTEGER, Sensitivity.NON_IDENTIFYING),
        _c("load_id", ColumnType.TEXT, Sensitivity.NON_IDENTIFYING),
    )

    facts = (
        FactTable(
            name="fact_encounter",
            grain=Grain.ENCOUNTER,
            description="One row per encounter.",
            columns=(
                *common,
                _c("encounter_key", ColumnType.KEY, Sensitivity.PSEUDONYM),
                _c("patient_key", ColumnType.KEY, Sensitivity.PSEUDONYM),
                _c("cohort_key", ColumnType.KEY, Sensitivity.PSEUDONYM),
                _c("organization_key", ColumnType.KEY, Sensitivity.PSEUDONYM),
                _c("encounter_class", ColumnType.TEXT, Sensitivity.NON_IDENTIFYING),
                _c("length_of_stay_days", ColumnType.DECIMAL, Sensitivity.NON_IDENTIFYING),
                _c("is_readmission", ColumnType.BOOLEAN, Sensitivity.NON_IDENTIFYING),
            ),
            dimension_keys={
                "cohort_key": "dim_cohort",
                "organization_key": "dim_organization",
                "date_key": "dim_date",
            },
        ),
        FactTable(
            name="fact_observation",
            grain=Grain.OBSERVATION,
            description="One row per observation. Not per patient.",
            columns=(
                *common,
                _c("observation_key", ColumnType.KEY, Sensitivity.PSEUDONYM),
                _c("patient_key", ColumnType.KEY, Sensitivity.PSEUDONYM),
                _c("cohort_key", ColumnType.KEY, Sensitivity.PSEUDONYM),
                _c("code_key", ColumnType.KEY, Sensitivity.PSEUDONYM),
                _c("organization_key", ColumnType.KEY, Sensitivity.PSEUDONYM),
                _c("value", ColumnType.DECIMAL, Sensitivity.NON_IDENTIFYING),
                _c("unit", ColumnType.TEXT, Sensitivity.NON_IDENTIFYING),
                _c("is_abnormal", ColumnType.BOOLEAN, Sensitivity.NON_IDENTIFYING),
                _c("status", ColumnType.TEXT, Sensitivity.NON_IDENTIFYING),
            ),
            dimension_keys={
                "cohort_key": "dim_cohort",
                "code_key": "dim_code",
                "organization_key": "dim_organization",
                "date_key": "dim_date",
            },
        ),
        FactTable(
            name="fact_adverse_event",
            grain=Grain.OBSERVATION,
            description="One row per suspected drug-event association, for pharmacovigilance.",
            columns=(
                *common,
                _c("event_key", ColumnType.KEY, Sensitivity.PSEUDONYM),
                _c("patient_key", ColumnType.KEY, Sensitivity.PSEUDONYM),
                _c("cohort_key", ColumnType.KEY, Sensitivity.PSEUDONYM),
                _c("drug_code_key", ColumnType.KEY, Sensitivity.PSEUDONYM),
                _c("event_code_key", ColumnType.KEY, Sensitivity.PSEUDONYM),
                _c("organization_key", ColumnType.KEY, Sensitivity.PSEUDONYM),
                _c("seriousness", ColumnType.TEXT, Sensitivity.NON_IDENTIFYING),
                _c("is_serious", ColumnType.BOOLEAN, Sensitivity.NON_IDENTIFYING),
            ),
            dimension_keys={
                "cohort_key": "dim_cohort",
                "drug_code_key": "dim_code",
                "event_code_key": "dim_code",
                "organization_key": "dim_organization",
                "date_key": "dim_date",
            },
        ),
        FactTable(
            name="fact_document_ingestion",
            grain=Grain.DOCUMENT,
            description="One row per ingested document.",
            columns=(
                *common,
                _c("document_key", ColumnType.KEY, Sensitivity.PSEUDONYM),
                _c("source_key", ColumnType.KEY, Sensitivity.PSEUDONYM),
                _c("organization_key", ColumnType.KEY, Sensitivity.PSEUDONYM),
                _c("document_type", ColumnType.TEXT, Sensitivity.NON_IDENTIFYING),
                _c("page_count", ColumnType.INTEGER, Sensitivity.NON_IDENTIFYING),
                _c("chunk_count", ColumnType.INTEGER, Sensitivity.NON_IDENTIFYING),
                _c("processing_ms", ColumnType.DECIMAL, Sensitivity.NON_IDENTIFYING),
                _c("used_ocr", ColumnType.BOOLEAN, Sensitivity.NON_IDENTIFYING),
                _c("quality_score", ColumnType.DECIMAL, Sensitivity.NON_IDENTIFYING),
                _c("succeeded", ColumnType.BOOLEAN, Sensitivity.NON_IDENTIFYING),
            ),
            dimension_keys={
                "source_key": "dim_source_system",
                "organization_key": "dim_organization",
                "date_key": "dim_date",
            },
        ),
        FactTable(
            name="fact_answer",
            grain=Grain.ANSWER,
            description="One row per copilot answer, for the usage dashboards.",
            columns=(
                *common,
                _c("answer_key", ColumnType.KEY, Sensitivity.PSEUDONYM),
                _c("role_key", ColumnType.KEY, Sensitivity.PSEUDONYM),
                _c("organization_key", ColumnType.KEY, Sensitivity.PSEUDONYM),
                _c("question_category", ColumnType.TEXT, Sensitivity.NON_IDENTIFYING),
                _c("response_mode", ColumnType.TEXT, Sensitivity.NON_IDENTIFYING),
                _c("latency_ms", ColumnType.DECIMAL, Sensitivity.NON_IDENTIFYING),
                _c("citation_count", ColumnType.INTEGER, Sensitivity.NON_IDENTIFYING),
                _c("is_grounded", ColumnType.BOOLEAN, Sensitivity.NON_IDENTIFYING),
                _c("was_abstained", ColumnType.BOOLEAN, Sensitivity.NON_IDENTIFYING),
            ),
            dimension_keys={
                "role_key": "dim_actor_role",
                "organization_key": "dim_organization",
                "date_key": "dim_date",
            },
        ),
        FactTable(
            name="fact_phi_access",
            grain=Grain.ACCESS_EVENT,
            description="One row per disclosure decision, for governance reporting.",
            columns=(
                *common,
                _c("access_key", ColumnType.KEY, Sensitivity.PSEUDONYM),
                _c("role_key", ColumnType.KEY, Sensitivity.PSEUDONYM),
                _c("organization_key", ColumnType.KEY, Sensitivity.PSEUDONYM),
                _c("purpose", ColumnType.TEXT, Sensitivity.NON_IDENTIFYING),
                _c("outcome", ColumnType.TEXT, Sensitivity.NON_IDENTIFYING),
                _c("was_break_glass", ColumnType.BOOLEAN, Sensitivity.NON_IDENTIFYING),
                _c("was_reviewed", ColumnType.BOOLEAN, Sensitivity.NON_IDENTIFYING),
            ),
            dimension_keys={
                "role_key": "dim_actor_role",
                "organization_key": "dim_organization",
                "date_key": "dim_date",
            },
        ),
        FactTable(
            name="fact_job_run",
            grain=Grain.JOB_RUN,
            description="One row per background job run, including de-identification jobs.",
            columns=(
                *common,
                _c("run_key", ColumnType.KEY, Sensitivity.PSEUDONYM),
                _c("organization_key", ColumnType.KEY, Sensitivity.PSEUDONYM),
                _c("job_kind", ColumnType.TEXT, Sensitivity.NON_IDENTIFYING),
                _c("status", ColumnType.TEXT, Sensitivity.NON_IDENTIFYING),
                _c("duration_ms", ColumnType.DECIMAL, Sensitivity.NON_IDENTIFYING),
                _c("rows_processed", ColumnType.INTEGER, Sensitivity.NON_IDENTIFYING),
                _c("rows_rejected", ColumnType.INTEGER, Sensitivity.NON_IDENTIFYING),
            ),
            dimension_keys={
                "organization_key": "dim_organization",
                "date_key": "dim_date",
            },
        ),
    )

    return WarehouseSchema(
        facts=facts,
        dimensions=(
            dim_date,
            dim_organization,
            dim_cohort,
            dim_code,
            dim_actor_role,
            dim_source_system,
        ),
    )


class Warehouse:
    """A tenant-scoped columnar store over a schema.

    Rows are validated against the schema on append. A warehouse that accepts whatever it is
    given is one where a unit change or a renamed column goes unnoticed until a chart looks odd
    a quarter later.
    """

    def __init__(self, schema: WarehouseSchema | None = None) -> None:
        self._schema = schema or default_schema()
        self._facts: dict[str, list[dict[str, Any]]] = {f.name: [] for f in self._schema.facts}
        self._dimensions: dict[str, dict[Any, dict[str, Any]]] = {
            d.name: {} for d in self._schema.dimensions
        }
        self._freshness: dict[str, Freshness] = {}

    @property
    def schema(self) -> WarehouseSchema:
        return self._schema

    def upsert_dimension(self, name: str, row: dict[str, Any]) -> None:
        """Insert or replace a dimension row, keyed on its surrogate."""
        dimension = self._schema.dimension(name)
        if dimension is None:
            raise SchemaError(f"unknown dimension {name!r}")
        key = row.get(dimension.key)
        if key is None:
            raise SchemaError(f"dimension row for {name!r} has no {dimension.key!r}")
        unknown = set(row) - {c.name for c in dimension.columns}
        if unknown:
            raise SchemaError(
                f"dimension {name!r} row has unknown columns {sorted(unknown)}; a column the "
                "schema does not declare cannot be queried and will be silently invisible"
            )
        self._dimensions[name][key] = dict(row)

    def append_facts(
        self,
        name: str,
        rows: list[dict[str, Any]],
        *,
        load_id: str,
        as_of: dt.datetime,
        ruleset_version: str = "",
    ) -> int:
        """Append validated fact rows and advance this fact's freshness."""
        fact = self._schema.fact(name)
        if fact is None:
            raise SchemaError(f"unknown fact table {name!r}")
        declared = {c.name for c in fact.columns}

        prepared: list[dict[str, Any]] = []
        for index, row in enumerate(rows):
            unknown = set(row) - declared
            if unknown:
                raise SchemaError(f"{name} row {index} has undeclared columns {sorted(unknown)}")
            for column in fact.columns:
                if not column.nullable and row.get(column.name) is None:
                    raise SchemaError(
                        f"{name} row {index} has no value for non-nullable {column.name!r}"
                    )
            stamped = dict(row)
            stamped["load_id"] = load_id
            prepared.append(stamped)

        self._facts[name].extend(prepared)
        previous = self._freshness.get(name)
        # Freshness only ever moves forward. A late-arriving backfill of older data must not
        # make the warehouse claim to be less current than it is.
        if previous is None or previous.as_of is None or as_of > previous.as_of:
            self._freshness[name] = Freshness(
                as_of=as_of, load_id=load_id, ruleset_version=ruleset_version
            )
        _log.debug("warehouse.appended", fact=name, rows=len(prepared), load=load_id)
        return len(prepared)

    def rows(self, fact_name: str, organization_id: str) -> Iterator[dict[str, Any]]:
        """Every row of a fact **for one organisation**.

        The organisation is a required argument rather than an optional filter. An optional
        filter is one a caller can omit, and the result of omitting it is a cross-tenant read.
        """
        if not organization_id.strip():
            raise SchemaError(
                "rows() requires an organization_id; an unscoped scan would cross tenants"
            )
        for row in self._facts.get(fact_name, []):
            if row.get("organization_id") == organization_id:
                yield row

    def dimension_row(self, name: str, key: Any) -> dict[str, Any] | None:
        return self._dimensions.get(name, {}).get(key)

    def freshness(self, fact_name: str) -> Freshness:
        return self._freshness.get(fact_name, Freshness(as_of=None))

    def row_count(self, fact_name: str, organization_id: str | None = None) -> int:
        rows = self._facts.get(fact_name, [])
        if organization_id is None:
            return len(rows)
        return sum(1 for r in rows if r.get("organization_id") == organization_id)

    def organizations(self) -> tuple[str, ...]:
        found: set[str] = set()
        for rows in self._facts.values():
            found.update(str(r.get("organization_id", "")) for r in rows)
        return tuple(sorted(o for o in found if o))

    def statistics(self) -> dict[str, Any]:
        return {
            "schemaVersion": self._schema.version,
            "facts": {name: len(rows) for name, rows in self._facts.items() if rows},
            "dimensions": {name: len(rows) for name, rows in self._dimensions.items() if rows},
            "organizations": len(self.organizations()),
        }
