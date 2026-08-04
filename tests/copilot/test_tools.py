"""Tool contract, registry mediation, and the clinical tools themselves.

The registry is the authorisation and validation choke point, so most of what matters here is
what it *refuses*.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from cip_copilot.domain import Evidence, EvidenceKind
from cip_copilot.records import (
    ConditionRecord,
    InMemoryClinicalData,
    MedicationRecord,
    ObservationRecord,
    PatientRecord,
)
from cip_copilot.tools.base import (
    SideEffect,
    ToolContext,
    ToolError,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    validate_arguments,
)
from cip_copilot.tools.clinical import (
    DiagnosisLookupTool,
    LabTrendTool,
    MedicationLookupTool,
    PatientLookupTool,
    RiskScoreTool,
    TimelineTool,
    build_clinical_toolset,
)

TENANT_A = uuid.UUID("11111111-1111-4111-8111-111111111111")
TENANT_B = uuid.UUID("22222222-2222-4222-8222-222222222222")
PATIENT = uuid.UUID("33333333-3333-4333-8333-333333333333")

ALL_SCOPES = frozenset({"patients:read", "reference:read", "analytics:read", "documents:read"})


@pytest.fixture
def data() -> InMemoryClinicalData:
    store = InMemoryClinicalData()
    store.add_patient(
        PatientRecord(
            patient_id=PATIENT,
            tenant_id=TENANT_A,
            display_name="Jordan Rivera",
            birth_date=dt.date(1955, 1, 1),
            sex="female",
        )
    )
    store.conditions.extend(
        [
            ConditionRecord(
                condition_id="c1",
                patient_id=PATIENT,
                display="Hypertension",
                onset=dt.date(2019, 4, 12),
            ),
            ConditionRecord(
                condition_id="c2",
                patient_id=PATIENT,
                display="Resolved pneumonia",
                onset=dt.date(2018, 1, 1),
                abatement=dt.date(2018, 2, 1),
                clinical_status="resolved",
            ),
        ]
    )
    store.medications.append(
        MedicationRecord(
            medication_id="m1",
            patient_id=PATIENT,
            display="Lisinopril",
            dose="10 mg",
            start_date=dt.date(2019, 5, 1),
        )
    )
    for index, (day, value) in enumerate(
        [(dt.date(2025, 9, 2), 4.1), (dt.date(2026, 3, 14), 5.4)], start=1
    ):
        store.observations.append(
            ObservationRecord(
                observation_id=f"k{index}",
                patient_id=PATIENT,
                display="Potassium",
                value=value,
                unit="mmol/L",
                effective=day,
                reference_low=3.5,
                reference_high=5.1,
            )
        )
    return store


@pytest.fixture
def context() -> ToolContext:
    return ToolContext(tenant_id=TENANT_A, scopes=ALL_SCOPES, patient_id=PATIENT)


class TestToolSpec:
    def test_name_must_be_snake_case(self) -> None:
        with pytest.raises(ValueError, match="lower_snake_case"):
            ToolSpec(name="PatientLookup", description="", parameters={"type": "object"})

    def test_parameters_must_be_an_object_schema(self) -> None:
        with pytest.raises(ValueError, match="JSON Schema object"):
            ToolSpec(name="ok", description="", parameters={"type": "string"})

    def test_a_write_tool_must_require_approval(self) -> None:
        """A write nobody approved must not be reachable from a planning decision alone."""
        with pytest.raises(ValueError, match="must require approval"):
            ToolSpec(
                name="ok",
                description="",
                parameters={"type": "object"},
                side_effect=SideEffect.WRITE,
            )


class TestArgumentValidation:
    @pytest.fixture
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="sample",
            description="",
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "count": {"type": "integer", "minimum": 1, "maximum": 5},
                    "mode": {"type": "string", "enum": ["a", "b"]},
                    "flag": {"type": "boolean"},
                    "items": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["name"],
            },
        )

    def test_accepts_valid_arguments(self, spec: ToolSpec) -> None:
        assert validate_arguments(spec, {"name": "x", "count": 3}) == {"name": "x", "count": 3}

    def test_rejects_missing_required(self, spec: ToolSpec) -> None:
        with pytest.raises(ToolError, match="Missing required"):
            validate_arguments(spec, {"count": 1})

    def test_rejects_unknown_arguments(self, spec: ToolSpec) -> None:
        """A planner emitting `patient` for `patient_id` has erred; dropping it silently
        would run the tool against the wrong thing."""
        with pytest.raises(ToolError, match="Unknown argument"):
            validate_arguments(spec, {"name": "x", "nme": "typo"})

    def test_rejects_wrong_type(self, spec: ToolSpec) -> None:
        with pytest.raises(ToolError, match="must be an integer"):
            validate_arguments(spec, {"name": "x", "count": "3"})

    def test_a_boolean_is_not_an_integer(self, spec: ToolSpec) -> None:
        """bool subclasses int in Python; accepting True as 1 turns a bad argument into a value."""
        with pytest.raises(ToolError, match="must be an integer"):
            validate_arguments(spec, {"name": "x", "count": True})

    @pytest.mark.parametrize("value", [0, 6])
    def test_enforces_bounds(self, spec: ToolSpec, value: int) -> None:
        with pytest.raises(ToolError, match="must be"):
            validate_arguments(spec, {"name": "x", "count": value})

    def test_enforces_enum(self, spec: ToolSpec) -> None:
        with pytest.raises(ToolError, match="must be one of"):
            validate_arguments(spec, {"name": "x", "mode": "c"})

    def test_validates_array_items(self, spec: ToolSpec) -> None:
        with pytest.raises(ToolError, match="must be a string"):
            validate_arguments(spec, {"name": "x", "items": ["ok", 5]})


class _ForeignTenantTool:
    """Returns evidence belonging to another tenant — a defect the registry must catch."""

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name="leaky", description="", parameters={"type": "object"})

    async def run(self, arguments: dict, *, context: ToolContext) -> ToolResult:
        return ToolResult(
            tool_name="leaky",
            evidence=(
                Evidence(
                    id="foreign",
                    kind=EvidenceKind.STRUCTURED_FACT,
                    content="another tenant's note",
                    tenant_id=TENANT_B,
                ),
            ),
        )


class TestRegistry:
    async def test_rejects_a_caller_without_the_required_scope(
        self, data: InMemoryClinicalData
    ) -> None:
        """A tool must not be able to widen its caller's access by being called."""
        registry = ToolRegistry([PatientLookupTool(data)])
        narrow = ToolContext(tenant_id=TENANT_A, scopes=frozenset({"reference:read"}))
        with pytest.raises(ToolError, match="requires scope 'patients:read'"):
            await registry.invoke("patient_lookup", {"patient_id": str(PATIENT)}, context=narrow)

    async def test_blocks_evidence_from_another_tenant(self, context: ToolContext) -> None:
        registry = ToolRegistry([_ForeignTenantTool()])
        with pytest.raises(ToolError, match="different tenant"):
            await registry.invoke("leaky", {}, context=context)

    def test_refuses_duplicate_registration(self, data: InMemoryClinicalData) -> None:
        """Silent replacement would let import order decide which implementation wins."""
        registry = ToolRegistry([PatientLookupTool(data)])
        with pytest.raises(ValueError, match="already registered"):
            registry.register(PatientLookupTool(data))

    def test_unknown_tool_is_an_error(self) -> None:
        with pytest.raises(ToolError, match="Unknown tool"):
            ToolRegistry().get("nope")

    def test_function_schemas_match_the_specs(self, data: InMemoryClinicalData) -> None:
        """One declaration drives our validation and a provider's, so they cannot drift."""
        registry = ToolRegistry([PatientLookupTool(data), DiagnosisLookupTool(data)])
        schemas = registry.as_function_schemas()
        assert [s["name"] for s in schemas] == ["diagnosis_lookup", "patient_lookup"]
        assert all(s["parameters"]["type"] == "object" for s in schemas)

    def test_toolset_omits_tools_whose_dependencies_are_absent(
        self, data: InMemoryClinicalData
    ) -> None:
        """A plan must not be built around a capability that will certainly fail."""
        names = {t.spec.name for t in build_clinical_toolset(source=data)}
        assert "drug_interaction_check" not in names
        assert "document_search" not in names
        assert "patient_lookup" in names


class TestClinicalTools:
    async def test_patient_lookup_reports_age(
        self, data: InMemoryClinicalData, context: ToolContext
    ) -> None:
        result = await PatientLookupTool(data).run({"patient_id": str(PATIENT)}, context=context)
        assert result.data["age"] >= 70
        assert result.evidence[0].kind is EvidenceKind.STRUCTURED_FACT

    async def test_patient_lookup_is_tenant_scoped(self, data: InMemoryClinicalData) -> None:
        other = ToolContext(tenant_id=TENANT_B, scopes=ALL_SCOPES)
        result = await PatientLookupTool(data).run({"patient_id": str(PATIENT)}, context=other)
        assert result.is_empty
        assert "No such patient" in result.note

    async def test_a_malformed_patient_id_is_refused(
        self, data: InMemoryClinicalData, context: ToolContext
    ) -> None:
        """Silently accepting it would report "no records" for a patient that cannot exist."""
        with pytest.raises(ToolError, match="not a valid patient id"):
            await PatientLookupTool(data).run({"patient_id": "not-a-uuid"}, context=context)

    async def test_diagnosis_lookup_can_filter_to_active(
        self, data: InMemoryClinicalData, context: ToolContext
    ) -> None:
        tool = DiagnosisLookupTool(data)
        every = await tool.run({"patient_id": str(PATIENT)}, context=context)
        active = await tool.run({"patient_id": str(PATIENT), "active_only": True}, context=context)
        assert len(every.evidence) == 2
        assert len(active.evidence) == 1

    async def test_lab_trend_detects_a_rise(
        self, data: InMemoryClinicalData, context: ToolContext
    ) -> None:
        result = await LabTrendTool(data).run(
            {"patient_id": str(PATIENT), "analyte": "potassium"}, context=context
        )
        assert result.data["direction"] == "rising"
        assert result.data["latest"] == 5.4
        assert "high" in result.data["flags"]

    async def test_a_single_result_is_not_a_trend(
        self, data: InMemoryClinicalData, context: ToolContext
    ) -> None:
        """Calling one reading "stable" would be an invention."""
        data.observations = [data.observations[0]]
        result = await LabTrendTool(data).run(
            {"patient_id": str(PATIENT), "analyte": "potassium"}, context=context
        )
        assert result.data["direction"] == "unknown"
        assert "at least two" in result.note

    async def test_missing_analyte_is_reported_not_guessed(
        self, data: InMemoryClinicalData, context: ToolContext
    ) -> None:
        result = await LabTrendTool(data).run(
            {"patient_id": str(PATIENT), "analyte": "troponin"}, context=context
        )
        assert result.is_empty
        assert "No troponin" in result.note

    async def test_risk_score_refuses_without_a_birth_date(
        self, data: InMemoryClinicalData, context: ToolContext
    ) -> None:
        """A partial score would be reported as a score."""
        from dataclasses import replace

        data.patients[PATIENT] = replace(data.patients[PATIENT], birth_date=None)
        result = await RiskScoreTool(data).run(
            {"patient_id": str(PATIENT), "score": "chads2_vasc"}, context=context
        )
        assert result.is_empty
        assert "birth date" in result.note

    async def test_risk_score_reports_its_components(
        self, data: InMemoryClinicalData, context: ToolContext
    ) -> None:
        result = await RiskScoreTool(data).run(
            {"patient_id": str(PATIENT), "score": "chads2_vasc"}, context=context
        )
        assert result.data["score"] >= 1
        assert "hypertension" in result.data["contributing"]
        assert "sex_female" in result.data["contributing"]

    async def test_timeline_orders_events(
        self, data: InMemoryClinicalData, context: ToolContext
    ) -> None:
        result = await TimelineTool(data).run({"patient_id": str(PATIENT)}, context=context)
        dates = [e.effective_date for e in result.evidence]
        assert dates == sorted(dates)

    async def test_medication_lookup_returns_rxnorm_codes(
        self, data: InMemoryClinicalData, context: ToolContext
    ) -> None:
        data.medications[0] = MedicationRecord(
            medication_id="m1",
            patient_id=PATIENT,
            display="Lisinopril",
            rxnorm_code="29046",
            start_date=dt.date(2019, 5, 1),
        )
        result = await MedicationLookupTool(data).run({"patient_id": str(PATIENT)}, context=context)
        assert result.data["rxnorm_codes"] == ["29046"]
