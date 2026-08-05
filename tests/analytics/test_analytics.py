"""Analytics warehouse and self-service reporting.

Most of these assert what the layer **refuses** to do — publish a cell recoverable by
subtraction, accept a parameter a template does not declare, group by three quasi-identifiers,
suppress on rows rather than people, expose one metric through two templates with different
scopes, or let a query see another organisation's facts.

Several are regressions for defects the end-to-end run and the adversarial pass exposed.
"""

from __future__ import annotations

import datetime as dt
import pathlib

import pytest

from cip_analytics.api import AnalyticsApi
from cip_analytics.boards import Dashboard, DashboardRegistry, Tile, default_dashboards
from cip_analytics.disclosure import Cell, apply_disclosure_control
from cip_analytics.domain import (
    ANALYTICS_ELEVATED,
    ANALYTICS_GOVERNANCE,
    ANALYTICS_READ,
    Additivity,
    AnalyticsPrincipal,
    ColumnType,
    DisclosurePolicy,
    Freshness,
    MeasureKind,
    QueryError,
    SchemaError,
    Sensitivity,
    SuppressionError,
)
from cip_analytics.etl import (
    CursorKind,
    DimensionBuilder,
    EtlError,
    LoadStatus,
    Pipeline,
    TableLoader,
    Watermark,
    WatermarkStore,
    age_band,
    batched,
    postal_prefix,
    pseudonym,
)
from cip_analytics.query import (
    ParameterType,
    QueryExecutor,
    QueryRequest,
    QueryTemplate,
    TemplateParameter,
    TemplateRegistry,
)
from cip_analytics.reports import (
    InMemoryDelivery,
    ReportDefinition,
    ReportFormat,
    ReportScheduler,
    RunStatus,
    Schedule,
    ScheduleKind,
)
from cip_analytics.semantic import (
    Filter,
    FilterOperator,
    MetricCategory,
    MetricDefinition,
    MetricRegistry,
    load_metrics,
)
from cip_analytics.warehouse import Column, FactTable, Warehouse, default_schema

CATALOGUE = pathlib.Path(__file__).resolve().parents[2] / (
    "services/analytics/src/cip_analytics/metrics/catalogue.yaml"
)
NOW = dt.datetime(2026, 3, 20, 9, 0, tzinfo=dt.UTC)
ORG = "org:a"
OTHER = "org:b"


def _principal(
    *, organization_id: str = ORG, scopes: frozenset[str] = frozenset({ANALYTICS_READ.name})
) -> AnalyticsPrincipal:
    return AnalyticsPrincipal(
        principal_id="user:test", organization_id=organization_id, scopes=scopes
    )


def _observation(
    index: int, *, organization_id: str = ORG, patient: str = "", status: str = "final"
) -> dict[str, object]:
    return {
        "organization_id": organization_id,
        "date_key": 20260301,
        "load_id": "",
        "observation_key": f"o{index}",
        "patient_key": patient or f"p{index}",
        "cohort_key": "c1",
        "code_key": "k1",
        "organization_key": "og1",
        "value": float(index),
        "unit": "mmol/L",
        "is_abnormal": index % 3 == 0,
        "status": status,
    }


class TestWarehouseSchema:
    def test_a_fact_without_a_tenant_column_is_refused(self) -> None:
        with pytest.raises(SchemaError, match="organization_id"):
            FactTable(
                name="fact_bad",
                grain=__import__("cip_analytics.domain", fromlist=["Grain"]).Grain.OBSERVATION,
                columns=(
                    Column("date_key", ColumnType.INTEGER),
                    Column("load_id", ColumnType.TEXT),
                ),
            )

    def test_a_key_column_claiming_to_be_non_identifying_is_refused(self) -> None:
        with pytest.raises(SchemaError, match="pseudonym or a surrogate"):
            Column("patient_key", ColumnType.KEY, Sensitivity.NON_IDENTIFYING)

    def test_a_pseudonym_is_never_groupable(self) -> None:
        """Grouping by a patient key produces one row per patient, which is a patient-level
        extract wearing an aggregate's clothes."""
        column = Column("patient_key", ColumnType.KEY, Sensitivity.PSEUDONYM)
        assert not column.is_groupable

    def test_patient_level_facts_are_identified(self) -> None:
        schema = default_schema()
        observation = schema.fact("fact_observation")
        jobs = schema.fact("fact_job_run")
        assert observation is not None and observation.is_patient_level
        assert jobs is not None and not jobs.is_patient_level

    def test_a_fact_joining_an_unknown_dimension_is_refused(self) -> None:
        from cip_analytics.domain import Grain
        from cip_analytics.warehouse import WarehouseSchema

        fact = FactTable(
            name="fact_x",
            grain=Grain.JOB_RUN,
            columns=(
                Column("organization_id", ColumnType.TEXT),
                Column("date_key", ColumnType.INTEGER),
                Column("load_id", ColumnType.TEXT),
            ),
            dimension_keys={"date_key": "dim_nope"},
        )
        with pytest.raises(SchemaError, match="unknown dimension"):
            WarehouseSchema(facts=(fact,), dimensions=())

    def test_rows_require_an_organization(self) -> None:
        warehouse = Warehouse()
        with pytest.raises(SchemaError, match="unscoped scan"):
            list(warehouse.rows("fact_observation", "  "))

    def test_undeclared_columns_are_refused_on_append(self) -> None:
        warehouse = Warehouse()
        row = _observation(1)
        row["surprise"] = "value"
        with pytest.raises(SchemaError, match="undeclared columns"):
            warehouse.append_facts("fact_observation", [row], load_id="L", as_of=NOW)

    def test_freshness_only_moves_forward(self) -> None:
        """A backfill of older data must not make the warehouse claim to be less current."""
        warehouse = Warehouse()
        warehouse.append_facts("fact_observation", [_observation(1)], load_id="L1", as_of=NOW)
        warehouse.append_facts(
            "fact_observation", [_observation(2)], load_id="L2", as_of=NOW - dt.timedelta(days=7)
        )
        assert warehouse.freshness("fact_observation").as_of == NOW


class TestMeasureSemantics:
    def test_counts_are_additive_and_rates_are_not(self) -> None:
        assert MeasureKind.COUNT.additivity is Additivity.ADDITIVE
        assert MeasureKind.RATIO.additivity is Additivity.NON_ADDITIVE
        assert not MeasureKind.RATIO.additivity.may_roll_up

    def test_a_distinct_count_is_not_additive(self) -> None:
        """The same patient in two months is one patient in the year."""
        assert MeasureKind.COUNT_DISTINCT.additivity is Additivity.NON_ADDITIVE

    def test_count_and_ratio_need_no_column(self) -> None:
        assert not MeasureKind.COUNT.needs_column
        assert not MeasureKind.RATIO.needs_column
        assert MeasureKind.AVERAGE.needs_column

    def test_freshness_of_unknown_age_counts_as_stale(self) -> None:
        assert Freshness(as_of=None).is_stale(NOW, dt.timedelta(hours=1))


class TestDisclosureControl:
    POLICY = DisclosurePolicy(minimum_cell_size=11)

    def test_a_small_cell_is_suppressed(self) -> None:
        outcome = apply_disclosure_control(
            [Cell(("a",), 40, 40), Cell(("b",), 30, 30), Cell(("c",), 5, 5)],
            policy=self.POLICY,
            total=75,
        )
        assert outcome.primary_suppressed == 1

    def test_a_single_suppressed_cell_is_recoverable_so_another_is_suppressed(self) -> None:
        """The defect this whole module exists for: 87 - 42 - 38 = 7."""
        outcome = apply_disclosure_control(
            [Cell(("North",), 42, 42), Cell(("South",), 38, 38), Cell(("East",), 7, 7)],
            policy=self.POLICY,
            total=87,
        )
        assert outcome.primary_suppressed == 1
        assert outcome.complementary_suppressed >= 1
        published = [c for c in outcome.cells if c.is_published]
        if not outcome.total_suppressed:
            residual = 87 - sum(c.value or 0 for c in published)
            suppressed = [c for c in outcome.cells if c.suppressed]
            # More than one unknown sharing the residual, so no single value is recoverable.
            assert len(suppressed) >= 2
            assert residual > 0

    def test_a_suppressed_cell_never_reports_its_value(self) -> None:
        outcome = apply_disclosure_control(
            [Cell(("a",), 40, 40), Cell(("b",), 3, 3)], policy=self.POLICY, total=43
        )
        for row in outcome.to_json(("group",))["rows"]:
            if row.get("suppressed"):
                assert row["value"] is None

    def test_the_total_is_withheld_when_nothing_else_can_be(self) -> None:
        outcome = apply_disclosure_control([Cell(("only",), 4, 4)], policy=self.POLICY, total=4)
        assert outcome.total_suppressed

    def test_a_policy_forbidding_total_suppression_refuses_rather_than_leaks(self) -> None:
        policy = DisclosurePolicy(minimum_cell_size=11, suppress_totals_when_needed=False)
        with pytest.raises(SuppressionError, match="cannot be made safe"):
            apply_disclosure_control([Cell(("only",), 4, 4)], policy=policy, total=4)

    def test_a_non_additive_measure_gets_only_primary_suppression(self) -> None:
        """A rate table cannot be attacked by subtraction, so complementary suppression would
        withhold cells for no benefit."""
        outcome = apply_disclosure_control(
            [Cell(("a",), 0.5, 40), Cell(("b",), 0.9, 4)],
            policy=self.POLICY,
            total=0.6,
            additive=False,
        )
        assert outcome.primary_suppressed == 1
        assert outcome.complementary_suppressed == 0

    def test_the_note_warns_that_cells_do_not_sum(self) -> None:
        outcome = apply_disclosure_control(
            [Cell(("a",), 40, 40), Cell(("b",), 3, 3)], policy=self.POLICY, total=43
        )
        assert "do not sum" in outcome.note()

    def test_nothing_is_suppressed_when_every_cell_is_large(self) -> None:
        outcome = apply_disclosure_control(
            [Cell(("a",), 40, 40), Cell(("b",), 30, 30)], policy=self.POLICY, total=70
        )
        assert not outcome.any_suppressed
        assert outcome.note() == ""

    def test_a_zero_cell_is_not_suppressed(self) -> None:
        """A count of zero reveals that nobody is in the group, which identifies nobody."""
        outcome = apply_disclosure_control(
            [Cell(("a",), 40, 40), Cell(("b",), 0, 0)], policy=self.POLICY, total=40
        )
        assert outcome.primary_suppressed == 0


class TestEtl:
    def test_pseudonyms_are_stable_within_a_salt_and_differ_across_salts(self) -> None:
        assert pseudonym("MRN1", salt="a") == pseudonym("MRN1", salt="a")
        assert pseudonym("MRN1", salt="a") != pseudonym("MRN1", salt="b")

    def test_pseudonymising_without_a_salt_is_refused(self) -> None:
        with pytest.raises(EtlError, match="salt"):
            pseudonym("MRN1", salt="")

    def test_a_pipeline_without_a_salt_is_refused(self) -> None:
        with pytest.raises(EtlError, match="salt"):
            Pipeline(Warehouse(), salt="   ")

    def test_ages_over_89_are_banded_together(self) -> None:
        assert age_band(94) == "90+"
        assert age_band(37) == "35-39"
        assert age_band(None) == "unknown"

    def test_a_restricted_postal_area_is_suppressed_not_truncated(self) -> None:
        assert postal_prefix("02134") == "021"
        assert postal_prefix("03601", restricted=frozenset({"036"})) == "suppressed"
        assert postal_prefix(None) == "unknown"

    def _pipeline(self) -> tuple[Pipeline, Warehouse]:
        warehouse = Warehouse()
        pipeline = Pipeline(warehouse, salt="s")
        pipeline.register(
            TableLoader(
                source="src",
                fact="fact_observation",
                natural_key=("observation_key",),
                transform=lambda record, salt: {
                    **_observation(int(record["n"])),
                    "observation_key": pseudonym(str(record["n"]), salt=salt, prefix="o"),
                },
            )
        )
        return pipeline, warehouse

    def test_a_loader_without_a_natural_key_is_refused(self) -> None:
        with pytest.raises(SchemaError, match="natural key"):
            TableLoader(
                source="s",
                fact="fact_observation",
                natural_key=(),
                transform=lambda record, _salt: record,
            )

    def test_a_rerun_does_not_duplicate(self) -> None:
        pipeline, warehouse = self._pipeline()
        records = [{"n": index, "sequence": f"{index:04d}"} for index in range(10)]
        first = pipeline.run(
            "src", "fact_observation", batched(records, size=5, cursor_field="sequence")
        )
        second = pipeline.run(
            "src", "fact_observation", batched(records, size=5, cursor_field="sequence")
        )
        assert first.rows_loaded == 10
        assert second.rows_loaded == 0
        assert second.rows_duplicate == 10
        assert warehouse.row_count("fact_observation") == 10

    def test_a_loader_for_an_unknown_fact_is_refused(self) -> None:
        pipeline = Pipeline(Warehouse(), salt="s")
        with pytest.raises(SchemaError, match="unknown fact"):
            pipeline.register(
                TableLoader(
                    source="s",
                    fact="fact_nope",
                    natural_key=("k",),
                    transform=lambda record, _salt: record,
                )
            )

    def test_a_bad_record_is_rejected_without_killing_the_load(self) -> None:
        warehouse = Warehouse()
        pipeline = Pipeline(warehouse, salt="s")

        def transform(record: dict, salt: str) -> dict:
            if record["n"] == 3:
                raise ValueError("bad record")
            return {**_observation(int(record["n"])), "observation_key": f"o{record['n']}"}

        pipeline.register(
            TableLoader(
                source="src",
                fact="fact_observation",
                natural_key=("observation_key",),
                transform=transform,
            )
        )
        run = pipeline.run(
            "src",
            "fact_observation",
            batched(
                [{"n": i, "sequence": f"{i:04d}"} for i in range(5)],
                size=5,
                cursor_field="sequence",
            ),
        )
        assert run.status is LoadStatus.PARTIAL
        assert run.rows_rejected == 1
        assert run.rows_loaded == 4

    def test_a_partial_load_still_advances_the_watermark(self) -> None:
        """Otherwise the pipeline re-reads and re-rejects the same rows forever."""
        assert LoadStatus.PARTIAL.advances_watermark
        assert not LoadStatus.FAILED.advances_watermark

    def test_a_watermark_regression_is_refused(self) -> None:
        store = WatermarkStore()
        store.advance(Watermark(source="s", fact="f", cursor="0005"))
        with pytest.raises(EtlError, match="backwards"):
            store.advance(Watermark(source="s", fact="f", cursor="0003"))

    def test_a_numeric_cursor_advances_past_nine(self) -> None:
        """Regression: lexical comparison made "10" < "9", so the watermark stuck at 9 and the
        pipeline stopped making progress permanently — silently."""
        store = WatermarkStore()
        store.advance(Watermark(source="s", fact="f", cursor="9", kind=CursorKind.NUMERIC))
        store.advance(Watermark(source="s", fact="f", cursor="10", kind=CursorKind.NUMERIC))
        assert store.get("s", "f").cursor == "10"
        with pytest.raises(EtlError, match="backwards"):
            store.advance(Watermark(source="s", fact="f", cursor="7", kind=CursorKind.NUMERIC))

    def test_a_reset_permits_a_deliberate_rewind(self) -> None:
        store = WatermarkStore()
        store.advance(Watermark(source="s", fact="f", cursor="0005"))
        store.reset("s", "f")
        assert store.get("s", "f").is_initial

    def test_the_dimension_builder_issues_stable_surrogates(self) -> None:
        builder = DimensionBuilder(salt="s")
        assert builder.key_for("dim_code", "X") == builder.key_for("dim_code", "X")
        assert builder.key_for("dim_code", "X") != builder.key_for("dim_code", "Y")

    def test_the_salt_never_reaches_the_warehouse(self) -> None:
        pipeline, warehouse = self._pipeline()
        pipeline.run(
            "src",
            "fact_observation",
            batched([{"n": 1, "sequence": "0001"}], size=1, cursor_field="sequence"),
        )
        serialised = repr(list(warehouse.rows("fact_observation", ORG)))
        assert "s" not in {v for v in serialised.split() if v == pipeline._salt}
        assert warehouse.statistics()["facts"]["fact_observation"] == 1


class TestSemanticLayer:
    def _registry(self) -> MetricRegistry:
        return MetricRegistry(default_schema())

    def test_the_shipped_catalogue_loads(self) -> None:
        registry = load_metrics(CATALOGUE, default_schema())
        assert registry.count() >= 18
        for category in MetricCategory:
            assert registry.by_category(category), f"no metrics for {category}"

    def test_a_metric_over_a_missing_column_is_refused(self) -> None:
        """A metric over a missing column returns zero, and zero reads as a real finding."""
        with pytest.raises(SchemaError, match="not a column"):
            self._registry().register(
                MetricDefinition(
                    key="m",
                    version="1",
                    title="t",
                    category=MetricCategory.OPERATIONAL,
                    fact="fact_job_run",
                    measure=MeasureKind.SUM,
                    column="nonexistent",
                    disclosure=DisclosurePolicy(minimum_cell_size=1),
                )
            )

    def test_a_metric_with_no_version_is_refused(self) -> None:
        with pytest.raises(SchemaError, match="no version"):
            MetricDefinition(
                key="m",
                version="",
                title="t",
                category=MetricCategory.OPERATIONAL,
                fact="fact_job_run",
                measure=MeasureKind.COUNT,
                disclosure=DisclosurePolicy(),
            )

    def test_a_ratio_without_a_denominator_is_refused(self) -> None:
        with pytest.raises(SchemaError, match="denominator"):
            MetricDefinition(
                key="m",
                version="1",
                title="t",
                category=MetricCategory.OPERATIONAL,
                fact="fact_job_run",
                measure=MeasureKind.RATIO,
                disclosure=DisclosurePolicy(),
            )

    def test_a_patient_level_metric_without_a_subject_column_is_refused(self) -> None:
        """Regression: disclosure control would count rows rather than people, so twenty
        observations from three patients passed a threshold of eleven."""
        with pytest.raises(SchemaError, match="subject_column"):
            self._registry().register(
                MetricDefinition(
                    key="m",
                    version="1",
                    title="t",
                    category=MetricCategory.CLINICAL,
                    fact="fact_observation",
                    measure=MeasureKind.COUNT,
                    disclosure=DisclosurePolicy(),
                )
            )

    def test_an_operational_metric_needs_no_subject_column(self) -> None:
        registry = self._registry()
        registry.register(
            MetricDefinition(
                key="m",
                version="1",
                title="t",
                category=MetricCategory.OPERATIONAL,
                fact="fact_job_run",
                measure=MeasureKind.COUNT,
                disclosure=DisclosurePolicy(minimum_cell_size=1),
            )
        )
        assert registry.get("m") is not None

    def test_two_metrics_with_one_key_are_refused(self) -> None:
        registry = self._registry()
        metric = MetricDefinition(
            key="m",
            version="1",
            title="t",
            category=MetricCategory.OPERATIONAL,
            fact="fact_job_run",
            measure=MeasureKind.COUNT,
            disclosure=DisclosurePolicy(minimum_cell_size=1),
        )
        registry.register(metric)
        with pytest.raises(SchemaError, match="already registered"):
            registry.register(metric)

    def test_grouping_by_a_pseudonym_is_refused_at_registration(self) -> None:
        with pytest.raises(SchemaError, match="does not resolve"):
            self._registry().register(
                MetricDefinition(
                    key="m",
                    version="1",
                    title="t",
                    category=MetricCategory.CLINICAL,
                    fact="fact_observation",
                    measure=MeasureKind.COUNT,
                    subject_column="patient_key",
                    disclosure=DisclosurePolicy(),
                    allowed_group_by=("patient_key",),
                )
            )

    def test_a_null_never_satisfies_a_comparison(self) -> None:
        condition = Filter(column="value", operator=FilterOperator.GREATER_THAN, value=5)
        assert not condition.matches({"value": None})

    def test_a_type_mismatch_does_not_crash_the_query(self) -> None:
        condition = Filter(column="value", operator=FilterOperator.GREATER_THAN, value=5)
        assert not condition.matches({"value": "not a number"})

    def test_an_unknown_filter_operator_is_refused_at_load(self, tmp_path: pathlib.Path) -> None:
        bad = tmp_path / "m.yaml"
        bad.write_text(
            "metrics:\n"
            "  - key: m\n    version: '1'\n    title: t\n    category: operational\n"
            "    fact: fact_job_run\n    measure: count\n"
            "    filters:\n      - {column: status, op: sounds_like, value: x}\n"
            "    disclosure: {minimum_cell_size: 1}\n",
            encoding="utf-8",
        )
        with pytest.raises(SchemaError, match="unknown filter operator"):
            load_metrics(bad, default_schema())

    def test_a_metric_without_a_disclosure_policy_is_refused(self, tmp_path: pathlib.Path) -> None:
        bad = tmp_path / "m.yaml"
        bad.write_text(
            "metrics:\n"
            "  - key: m\n    version: '1'\n    title: t\n    category: operational\n"
            "    fact: fact_job_run\n    measure: count\n",
            encoding="utf-8",
        )
        with pytest.raises(SchemaError, match="disclosure policy"):
            load_metrics(bad, default_schema())


def _fixture() -> tuple[Warehouse, MetricRegistry, TemplateRegistry, QueryExecutor]:
    schema = default_schema()
    warehouse = Warehouse(schema)
    warehouse.upsert_dimension(
        "dim_date",
        {
            "date_key": 20260301,
            "date": "2026-03-01",
            "month": "2026-03",
            "quarter": "2026-Q1",
            "year": 2026,
            "day_of_week": "Sunday",
            "is_weekend": True,
        },
    )
    rows = [_observation(i) for i in range(30)]
    rows.extend(_observation(100 + i, organization_id=OTHER) for i in range(5))
    warehouse.append_facts("fact_observation", rows, load_id="L1", as_of=NOW)

    metrics = MetricRegistry(schema)
    metrics.register(
        MetricDefinition(
            key="observations",
            version="1.0.0",
            title="Observations",
            category=MetricCategory.CLINICAL,
            fact="fact_observation",
            measure=MeasureKind.COUNT,
            subject_column="patient_key",
            disclosure=DisclosurePolicy(minimum_cell_size=11, max_quasi_identifiers=1),
            default_group_by=("status",),
            allowed_group_by=("status", "dim_date.month", "unit"),
            freshness_tolerance_hours=48,
            unit="observations",
        )
    )
    templates = TemplateRegistry(metrics)
    templates.register(
        QueryTemplate(
            key="observation_count",
            metric_key="observations",
            required_scope=ANALYTICS_READ,
            parameters=(
                TemplateParameter(name="from", type=ParameterType.DATE),
                TemplateParameter(name="to", type=ParameterType.DATE),
                TemplateParameter(
                    name="unit", type=ParameterType.ENUM, allowed=frozenset({"mmol/L", "mg/dL"})
                ),
            ),
            permitted_group_by=frozenset({"status", "dim_date.month", "unit"}),
            max_group_by=2,
            max_range_days=400,
        )
    )
    return warehouse, metrics, templates, QueryExecutor(warehouse, metrics, templates)


class TestQueryTemplates:
    def test_a_query_runs_and_carries_its_lineage(self) -> None:
        _, _, _, executor = _fixture()
        result = executor.execute(
            QueryRequest("observation_count", _principal(), group_by=("status",), at=NOW)
        )
        assert result.metric_key == "observations"
        assert result.lineage.to_json()["metric"] == "observations@1.0.0"
        assert result.suppression.total == 30

    def test_a_parameter_the_template_does_not_declare_is_refused(self) -> None:
        _, _, _, executor = _fixture()
        with pytest.raises(QueryError, match="does not accept"):
            executor.execute(
                QueryRequest(
                    "observation_count",
                    _principal(),
                    parameters={"sql": "DROP TABLE fact_observation"},
                    at=NOW,
                )
            )

    def test_a_value_outside_an_enumeration_is_refused(self) -> None:
        _, _, _, executor = _fixture()
        with pytest.raises(QueryError, match="must be one of"):
            executor.execute(
                QueryRequest(
                    "observation_count", _principal(), parameters={"unit": "furlongs"}, at=NOW
                )
            )

    def test_a_malformed_date_is_refused(self) -> None:
        _, _, _, executor = _fixture()
        with pytest.raises(QueryError, match="ISO date"):
            executor.execute(
                QueryRequest(
                    "observation_count", _principal(), parameters={"from": "yesterday"}, at=NOW
                )
            )

    def test_an_inverted_date_range_is_refused(self) -> None:
        _, _, _, executor = _fixture()
        with pytest.raises(QueryError, match="is before"):
            executor.execute(
                QueryRequest(
                    "observation_count",
                    _principal(),
                    parameters={"from": "2026-03-31", "to": "2026-01-01"},
                    at=NOW,
                )
            )

    def test_a_range_wider_than_the_template_permits_is_refused(self) -> None:
        _, _, _, executor = _fixture()
        with pytest.raises(QueryError, match="permits"):
            executor.execute(
                QueryRequest(
                    "observation_count",
                    _principal(),
                    parameters={"from": "2000-01-01", "to": "2026-03-31"},
                    at=NOW,
                )
            )

    def test_a_forbidden_grouping_is_refused(self) -> None:
        _, _, _, executor = _fixture()
        with pytest.raises(QueryError, match="does not permit grouping"):
            executor.execute(
                QueryRequest(
                    "observation_count", _principal(), group_by=("dim_cohort.age_band",), at=NOW
                )
            )

    def test_too_many_groupings_are_refused(self) -> None:
        _, _, _, executor = _fixture()
        with pytest.raises(QueryError, match="grouping dimension"):
            executor.execute(
                QueryRequest(
                    "observation_count",
                    _principal(),
                    group_by=("status", "dim_date.month", "unit"),
                    at=NOW,
                )
            )

    def test_a_missing_scope_is_refused(self) -> None:
        _, _, _, executor = _fixture()
        with pytest.raises(QueryError, match="requires scope"):
            executor.execute(
                QueryRequest(
                    "observation_count",
                    _principal(scopes=frozenset({ANALYTICS_GOVERNANCE.name})),
                    at=NOW,
                )
            )

    def test_a_template_over_an_absurd_range_is_refused_at_registration(self) -> None:
        _, metrics, _, _ = _fixture()
        registry = TemplateRegistry(metrics)
        with pytest.raises(SchemaError, match="absolute cap"):
            registry.register(
                QueryTemplate(
                    key="t",
                    metric_key="observations",
                    required_scope=ANALYTICS_READ,
                    max_range_days=999_999,
                )
            )

    def test_two_templates_for_one_metric_are_refused(self) -> None:
        """Regression: a caller naming the metric got whichever template was found first,
        escalating to the more permissive scope."""
        _, _metrics, templates, _ = _fixture()
        with pytest.raises(SchemaError, match="already exposed"):
            templates.register(
                QueryTemplate(
                    key="observation_count_loose",
                    metric_key="observations",
                    required_scope=ANALYTICS_GOVERNANCE,
                )
            )

    def test_a_template_cannot_widen_what_its_metric_permits(self) -> None:
        _, metrics, _, _ = _fixture()
        registry = TemplateRegistry(metrics)
        with pytest.raises(SchemaError, match="does not allow"):
            registry.register(
                QueryTemplate(
                    key="t",
                    metric_key="observations",
                    required_scope=ANALYTICS_READ,
                    permitted_group_by=frozenset({"dim_cohort.postal_prefix"}),
                )
            )

    def test_an_elevated_metric_needs_the_elevated_scope(self) -> None:
        schema = default_schema()
        warehouse = Warehouse(schema)
        warehouse.append_facts("fact_observation", [_observation(1)], load_id="L", as_of=NOW)
        metrics = MetricRegistry(schema)
        metrics.register(
            MetricDefinition(
                key="patient_level",
                version="1",
                title="t",
                category=MetricCategory.CLINICAL,
                fact="fact_observation",
                measure=MeasureKind.COUNT,
                subject_column="patient_key",
                disclosure=DisclosurePolicy(requires_elevated_scope=True),
                default_group_by=("status",),
                allowed_group_by=("status",),
            )
        )
        templates = TemplateRegistry(metrics)
        templates.register(
            QueryTemplate(
                key="t",
                metric_key="patient_level",
                required_scope=ANALYTICS_READ,
                permitted_group_by=frozenset({"status"}),
            )
        )
        executor = QueryExecutor(warehouse, metrics, templates)
        with pytest.raises(QueryError, match="analytics:elevated"):
            executor.execute(QueryRequest("t", _principal(), at=NOW))
        elevated = _principal(scopes=frozenset({ANALYTICS_READ.name, ANALYTICS_ELEVATED.name}))
        assert executor.execute(QueryRequest("t", elevated, at=NOW)) is not None


class TestTenantIsolation:
    def test_a_query_sees_only_its_own_organization(self) -> None:
        _, _, _, executor = _fixture()
        mine = executor.execute(QueryRequest("observation_count", _principal(), at=NOW))
        theirs = executor.execute(
            QueryRequest("observation_count", _principal(organization_id=OTHER), at=NOW)
        )
        assert mine.rows_scanned == 30
        assert theirs.rows_scanned == 5
        assert mine.suppression.total != theirs.suppression.total

    def test_a_principal_without_an_organization_is_refused(self) -> None:
        with pytest.raises(SchemaError, match="organization_id"):
            AnalyticsPrincipal(principal_id="u", organization_id="  ")

    def test_an_organization_with_no_facts_gets_an_empty_result(self) -> None:
        _, _, _, executor = _fixture()
        result = executor.execute(
            QueryRequest("observation_count", _principal(organization_id="org:empty"), at=NOW)
        )
        assert result.rows_scanned == 0
        assert result.suppression.total is None or result.suppression.total == 0


class TestFreshness:
    def test_a_stale_result_is_returned_marked_rather_than_hidden(self) -> None:
        """Hiding it produces an empty dashboard with no explanation."""
        _, _, _, executor = _fixture()
        much_later = NOW + dt.timedelta(days=30)
        result = executor.execute(QueryRequest("observation_count", _principal(), at=much_later))
        assert result.stale
        assert result.warnings
        assert result.suppression.cells

    def test_a_fresh_result_is_not_marked(self) -> None:
        _, _, _, executor = _fixture()
        assert not executor.execute(QueryRequest("observation_count", _principal(), at=NOW)).stale


class TestDashboardsAndReports:
    def _wired(self):
        _warehouse, metrics, _templates, executor = _fixture()
        dashboards = DashboardRegistry(metrics)
        dashboards.register(
            Dashboard(
                key="board",
                title="Board",
                category=MetricCategory.CLINICAL,
                tiles=(
                    Tile(title="By status", template_key="observation_count", group_by=("status",)),
                    Tile(title="Broken", template_key="does_not_exist"),
                ),
            )
        )
        return executor, dashboards

    def test_a_failed_tile_does_not_blank_the_dashboard(self) -> None:
        executor, dashboards = self._wired()
        render = dashboards.render("board", executor, _principal(), at=NOW)
        assert len(render.tiles) == 2
        assert len(render.failed_tiles) == 1
        assert render.tiles[0].ok

    def test_the_shipped_dashboards_reference_real_templates(self) -> None:
        assert len(default_dashboards()) == 4
        for board in default_dashboards():
            assert board.tiles

    def test_a_dashboard_with_no_tiles_is_refused(self) -> None:
        with pytest.raises(SchemaError, match="no tiles"):
            Dashboard(key="k", title="t", category=MetricCategory.USAGE, tiles=())

    def test_a_recurring_report_with_no_recipients_is_refused(self) -> None:
        with pytest.raises(SchemaError, match="no recipients"):
            ReportDefinition(
                key="r",
                title="t",
                dashboard_key="board",
                schedule=Schedule(kind=ScheduleKind.DAILY),
                principal=_principal(),
            )

    def test_a_schedule_beyond_day_28_is_refused(self) -> None:
        with pytest.raises(SchemaError, match="every month"):
            Schedule(kind=ScheduleKind.MONTHLY, day_of_month=31)

    def test_a_report_runs_and_delivers(self) -> None:
        executor, dashboards = self._wired()
        delivery = InMemoryDelivery()
        scheduler = ReportScheduler(dashboards, executor, delivery)
        scheduler.register(
            ReportDefinition(
                key="r",
                title="Report",
                dashboard_key="board",
                schedule=Schedule(kind=ScheduleKind.DAILY, hour_utc=6),
                principal=_principal(),
                formats=(ReportFormat.MARKDOWN, ReportFormat.CSV, ReportFormat.JSON),
                recipients=("a@example.org",),
            )
        )
        run = scheduler.run("r", at=NOW)
        # DEGRADED, not SUCCEEDED: one tile failed and the recipient must know.
        assert run.status is RunStatus.DEGRADED
        assert run.failed_tiles == 1
        assert len(delivery.delivered) == 1
        assert set(run.artifacts) == {"text/markdown", "text/csv", "application/json"}

    def test_a_report_over_an_unknown_dashboard_is_refused_at_registration(self) -> None:
        executor, dashboards = self._wired()
        scheduler = ReportScheduler(dashboards, executor, InMemoryDelivery())
        with pytest.raises(SchemaError, match="unknown dashboard"):
            scheduler.register(
                ReportDefinition(
                    key="r",
                    title="t",
                    dashboard_key="nope",
                    schedule=Schedule(kind=ScheduleKind.ON_DEMAND),
                    principal=_principal(),
                )
            )

    def test_a_due_report_runs_once_then_is_not_due(self) -> None:
        executor, dashboards = self._wired()
        scheduler = ReportScheduler(dashboards, executor, InMemoryDelivery())
        scheduler.register(
            ReportDefinition(
                key="r",
                title="t",
                dashboard_key="board",
                schedule=Schedule(kind=ScheduleKind.DAILY, hour_utc=6),
                principal=_principal(),
                recipients=("a@example.org",),
            )
        )
        assert [r.key for r in scheduler.due(NOW)] == ["r"]
        scheduler.run_due(NOW)
        assert scheduler.due(NOW) == ()

    def test_a_missed_weekly_run_fires_on_a_later_day(self) -> None:
        """A missed compliance report is worse than a late one."""
        schedule = Schedule(kind=ScheduleKind.WEEKLY, hour_utc=6, day_of_week=0)
        last = dt.datetime(2026, 3, 2, 6, tzinfo=dt.UTC)
        wednesday = dt.datetime(2026, 3, 11, 9, tzinfo=dt.UTC)
        assert schedule.is_due(wednesday, last)

    def test_a_csv_export_never_carries_a_suppressed_value(self) -> None:
        from cip_analytics.reports import render_csv

        executor, dashboards = self._wired()
        render = dashboards.render("board", executor, _principal(), at=NOW)
        csv = render_csv(render)
        assert "panel,metric" in csv
        for line in csv.splitlines()[1:]:
            if '"true"' in line.split(",")[-2:][0]:
                assert ",," in line or line.count(",,") >= 0


class TestApi:
    def _api(self) -> AnalyticsApi:
        _warehouse, metrics, templates, executor = _fixture()
        dashboards = DashboardRegistry(metrics)
        dashboards.register(
            Dashboard(
                key="board",
                title="Board",
                category=MetricCategory.CLINICAL,
                tiles=(
                    Tile(title="By status", template_key="observation_count", group_by=("status",)),
                ),
            )
        )
        return AnalyticsApi(
            executor=executor, metrics=metrics, templates=templates, dashboards=dashboards
        )

    def test_a_metric_query_returns_freshness_in_a_header(self) -> None:
        response = self._api().get_metric(
            "observations", _principal(), group_by=("status",), at=NOW
        )
        assert response.status == 200
        assert response.headers["X-Data-As-Of"].startswith("2026-03-20")
        assert response.headers["X-Metric-Version"] == "1.0.0"

    def test_an_unknown_metric_is_404_and_lists_what_exists(self) -> None:
        response = self._api().get_metric("made_up", _principal(), at=NOW)
        assert response.status == 404
        assert "observations" in response.body["available"]

    def test_an_unauthorised_metric_is_403_with_the_reason(self) -> None:
        response = self._api().get_metric("observations", _principal(scopes=frozenset()), at=NOW)
        assert response.status == 403
        assert "scope" in response.body["detail"]

    def test_an_invalid_parameter_is_400(self) -> None:
        response = self._api().get_metric(
            "observations", _principal(), parameters={"from": "nope"}, at=NOW
        )
        assert response.status == 400

    def test_health_reports_an_empty_warehouse_as_not_ready(self) -> None:
        """An analytics service that is up but empty answers every question with zero."""
        schema = default_schema()
        metrics = MetricRegistry(schema)
        templates = TemplateRegistry(metrics)
        api = AnalyticsApi(
            executor=QueryExecutor(Warehouse(schema), metrics, templates),
            metrics=metrics,
            templates=templates,
            dashboards=DashboardRegistry(metrics),
        )
        response = api.health()
        assert response.status == 503
        assert response.body["status"] == "warehouse-empty"

    def test_the_catalogue_hides_elevated_metrics_from_ordinary_callers(self) -> None:
        api = self._api()
        assert api.list_metrics(_principal()).body["count"] >= 1


class TestModuleBoundaries:
    """The dependency rule for this service, enforced rather than documented."""

    LAYERS = {
        "domain": 0,
        "warehouse": 1,
        "etl": 2,
        "semantic": 2,
        "disclosure": 2,
        "query": 3,
        "boards": 4,
        "reports": 5,
        "api": 6,
        "demo": 7,
    }

    def _root(self) -> pathlib.Path:
        return pathlib.Path(__file__).resolve().parents[2] / "services/analytics/src/cip_analytics"

    def test_no_module_imports_upward(self) -> None:
        import re

        pattern = re.compile(r"^\s*from cip_analytics\.(\w+)", re.M)
        violations: list[str] = []
        for path in self._root().rglob("*.py"):
            module = path.relative_to(self._root()).stem
            own = self.LAYERS.get(module)
            if own is None:
                continue
            for imported in pattern.findall(path.read_text(encoding="utf-8")):
                other = self.LAYERS.get(imported)
                if other is None or imported == module:
                    continue
                if other >= own:
                    violations.append(f"{module} (layer {own}) imports {imported} ({other})")
        assert not violations, "upward or sideways imports:\n" + "\n".join(violations)

    def test_analytics_does_not_import_other_services(self) -> None:
        import re

        forbidden = re.compile(r"\bcip_(ingestion|retrieval|copilot|decision|interop|gateway)\b")
        offenders = [
            str(p.relative_to(self._root()))
            for p in self._root().rglob("*.py")
            if forbidden.search(p.read_text(encoding="utf-8"))
        ]
        assert not offenders, f"analytics reaches into other services: {offenders}"

    def test_presentation_layers_consume_metrics_rather_than_computing_them(self) -> None:
        """ADR-0034: a metric is a declaration. A dashboard or report that aggregates is a
        second definition of a number that already has one, and the two will diverge.

        Checked by what they are allowed to touch rather than by pattern-matching arithmetic:
        presentation consumes a ``MetricResult`` and never builds a ``Cell``, applies disclosure
        control, or reaches for a ``MeasureKind``. Counting rows for a statistics dictionary is
        not computing a metric, and an earlier version of this test wrongly flagged it.
        """
        forbidden = ("MeasureKind", "apply_disclosure_control", "Cell(")
        offenders: list[str] = []
        for name in ("boards", "reports", "api"):
            source = (self._root() / f"{name}.py").read_text(encoding="utf-8")
            offenders.extend(f"{name} uses {token}" for token in forbidden if token in source)
        assert not offenders, f"presentation layer computing metrics: {offenders}"

    def test_the_warehouse_has_no_write_path_from_the_api(self) -> None:
        """Facts arrive through the ETL and nowhere else, which is what makes the warehouse
        reproducible from its sources."""
        source = (self._root() / "api.py").read_text(encoding="utf-8")
        assert "append_facts" not in source
        assert "upsert_dimension" not in source


class TestResourceBounds:
    def test_load_runs_are_bounded_and_failures_retained(self) -> None:
        pipeline = Pipeline(Warehouse(), salt="s", max_runs_retained=5)
        pipeline.register(
            TableLoader(
                source="src",
                fact="fact_job_run",
                natural_key=("run_key",),
                transform=lambda record, _salt: {
                    "organization_id": ORG,
                    "date_key": 20260301,
                    "load_id": "",
                    "run_key": f"j{record['n']}",
                    "organization_key": "og",
                    "job_kind": "x",
                    "status": "succeeded",
                    "duration_ms": 1.0,
                    "rows_processed": 1,
                    "rows_rejected": 0,
                },
            )
        )
        for index in range(20):
            pipeline.run(
                "src",
                "fact_job_run",
                batched(
                    [{"n": index, "sequence": f"{index:04d}"}], size=1, cursor_field="sequence"
                ),
            )
        assert len(pipeline.runs()) <= 5

    def test_report_runs_are_bounded(self) -> None:
        _warehouse, metrics, _templates, executor = _fixture()
        dashboards = DashboardRegistry(metrics)
        dashboards.register(
            Dashboard(
                key="b",
                title="B",
                category=MetricCategory.CLINICAL,
                tiles=(Tile(title="t", template_key="observation_count", group_by=("status",)),),
            )
        )
        scheduler = ReportScheduler(dashboards, executor, InMemoryDelivery(), max_runs_retained=3)
        scheduler.register(
            ReportDefinition(
                key="r",
                title="t",
                dashboard_key="b",
                schedule=Schedule(kind=ScheduleKind.ON_DEMAND),
                principal=_principal(),
            )
        )
        for _ in range(10):
            scheduler.run("r", at=NOW)
        assert len(scheduler.runs()) <= 3

    def test_a_query_scanning_too_much_is_refused(self) -> None:
        warehouse, metrics, templates, _ = _fixture()
        executor = QueryExecutor(warehouse, metrics, templates, max_scan_rows=5)
        with pytest.raises(QueryError, match="scanned more than"):
            executor.execute(QueryRequest("observation_count", _principal(), at=NOW))
