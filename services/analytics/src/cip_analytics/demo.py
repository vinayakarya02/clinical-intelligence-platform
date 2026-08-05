"""End-to-end demonstration, disclosure attacks, and benchmarks.

Loads a synthetic population through the de-identifying ETL, computes every metric, renders the
four dashboards, runs a scheduled report, attacks the disclosure control, and benchmarks each
layer.

``python -m cip_analytics.demo`` reproduces the Phase 7 verification run.

**The clinical content here is synthetic.** Adverse-event codes, rates, and cohort sizes are
generated to exercise the machinery and mean nothing clinically.
"""

from __future__ import annotations

import datetime as dt
import pathlib
import random
import time
import tracemalloc
from dataclasses import dataclass
from typing import Any

from cip_analytics.api import AnalyticsApi
from cip_analytics.boards import DashboardRegistry, default_dashboards
from cip_analytics.disclosure import Cell, apply_disclosure_control
from cip_analytics.domain import (
    ANALYTICS_GOVERNANCE,
    ANALYTICS_READ,
    AnalyticsPrincipal,
    DisclosurePolicy,
)
from cip_analytics.etl import (
    DimensionBuilder,
    Pipeline,
    TableLoader,
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
    Schedule,
    ScheduleKind,
)
from cip_analytics.semantic import MetricCategory, load_metrics
from cip_analytics.warehouse import Warehouse, default_schema

CATALOGUE = pathlib.Path(__file__).parent / "metrics" / "catalogue.yaml"
NOW = dt.datetime(2026, 3, 20, 9, 0, tzinfo=dt.UTC)
ORG = "org:mercy-general"
OTHER_ORG = "org:st-elsewhere"
SALT = "warehouse-salt-2026-Q1"

#: Restricted three-digit ZIP prefixes, mirroring the Safe Harbor list used in Phase 6.
RESTRICTED_ZIPS = frozenset({"036", "059", "063", "102", "203", "556", "692", "790", "821"})

DRUGS = [
    ("RX001", "Lisinopril"),
    ("RX002", "Metformin"),
    ("RX003", "Simvastatin"),
    ("RX004", "Warfarin"),
    ("RX005", "Spironolactone"),
]
EVENTS = [
    ("EV01", "Angioedema"),
    ("EV02", "Hypoglycaemia"),
    ("EV03", "Myopathy"),
    ("EV04", "Haemorrhage"),
    ("EV05", "Hyperkalaemia"),
]
ANALYTES = [("2951-2", "Sodium"), ("2823-3", "Potassium"), ("2160-0", "Creatinine")]
SOURCES = [("MERCY-ADT", "adt"), ("MERCY-LAB", "lab"), ("STE-REG", "registration")]


@dataclass
class Bench:
    name: str
    operations: int
    seconds: float

    def render(self) -> str:
        per = (self.seconds / self.operations) * 1_000_000 if self.operations else 0.0
        rate = self.operations / self.seconds if self.seconds else 0.0
        return f"  {self.name:<46} {rate:>12,.0f} op/s {per:>10.2f} us each"


def build_templates(metrics: Any) -> TemplateRegistry:
    """One template per metric.

    Each declares its own scope, parameter bounds, and permitted groupings. A metric with no
    template cannot be queried at all, which is deliberate: publishing a definition and
    publishing a query surface are separate decisions.
    """
    registry = TemplateRegistry(metrics)
    date_params = (
        TemplateParameter(name="from", type=ParameterType.DATE, description="Inclusive start"),
        TemplateParameter(name="to", type=ParameterType.DATE, description="Inclusive end"),
    )

    definitions = [
        ("adverse_event_trend", "adverse_event_count", ANALYTICS_READ, 400),
        ("serious_event_rate", "serious_adverse_event_rate", ANALYTICS_READ, 400),
        ("cohort_size", "cohort_patient_count", ANALYTICS_READ, 400),
        ("abnormal_rate", "abnormal_result_rate", ANALYTICS_READ, 400),
        ("length_of_stay", "mean_length_of_stay", ANALYTICS_READ, 400),
        ("ingestion_volume", "documents_ingested", ANALYTICS_READ, 400),
        ("ingestion_quality", "ingestion_success_rate", ANALYTICS_READ, 400),
        ("ingestion_latency", "ingestion_p95_latency", ANALYTICS_READ, 400),
        ("extraction_quality", "mean_extraction_quality", ANALYTICS_READ, 400),
        ("job_failures", "etl_job_failure_count", ANALYTICS_READ, 400),
        ("phi_access_summary", "phi_access_count", ANALYTICS_GOVERNANCE, 400),
        ("break_glass_summary", "break_glass_count", ANALYTICS_GOVERNANCE, 400),
        ("break_glass_review", "break_glass_review_rate", ANALYTICS_GOVERNANCE, 400),
        ("deid_job_status", "deidentification_job_status", ANALYTICS_GOVERNANCE, 400),
        ("answer_volume", "answers_produced", ANALYTICS_READ, 400),
        ("grounding_rate", "grounding_pass_rate", ANALYTICS_READ, 400),
        ("abstention", "abstention_rate", ANALYTICS_READ, 400),
        ("answer_latency", "answer_p95_latency", ANALYTICS_READ, 400),
    ]

    for key, metric_key, scope, max_days in definitions:
        metric = metrics.require(metric_key)
        registry.register(
            QueryTemplate(
                key=key,
                metric_key=metric_key,
                required_scope=scope,
                parameters=date_params,
                permitted_group_by=frozenset(metric.allowed_group_by),
                max_group_by=2,
                max_range_days=max_days,
                description=metric.title,
            )
        )
    return registry


def _rng() -> random.Random:
    """Seeded, so the verification run is reproducible.

    A demo whose numbers move between runs cannot be used to check a change did nothing.
    """
    return random.Random(20260320)


def generate_sources(count: int = 900) -> dict[str, list[dict[str, Any]]]:
    """A synthetic operational extract, before de-identification.

    Deliberately holds raw identifiers — names, medical record numbers, exact dates, full postal
    codes — because the point is that the ETL removes them and the warehouse never sees them.
    """
    rng = _rng()
    start = dt.date(2025, 10, 1)
    patients = [
        {
            "mrn": f"MRN{index:06d}",
            "name": f"Patient {index}",
            "birth_date": dt.date(
                1940 + rng.randrange(0, 70), rng.randrange(1, 13), 1 + rng.randrange(0, 28)
            ),
            "sex": rng.choice(["male", "female"]),
            "postal_code": rng.choice(["02134", "02139", "03601", "10025", "90210", "60614"]),
            "organization_id": ORG if index % 5 else OTHER_ORG,
        }
        for index in range(220)
    ]

    encounters, observations, adverse, documents, answers, accesses, jobs = ([] for _ in range(7))

    for index in range(count):
        patient = patients[rng.randrange(len(patients))]
        when = start + dt.timedelta(days=rng.randrange(0, 170))
        org = patient["organization_id"]

        encounters.append(
            {
                "sequence": f"{index:08d}",
                "encounter_id": f"ENC{index:06d}",
                "patient": patient,
                "date": when,
                "organization_id": org,
                "encounter_class": rng.choice(["IMP", "AMB", "EMER"]),
                "length_of_stay_days": round(rng.uniform(0.5, 12.0), 1),
                "is_readmission": rng.random() < 0.12,
            }
        )
        code, display = ANALYTES[index % len(ANALYTES)]
        observations.append(
            {
                "sequence": f"{index:08d}",
                "observation_id": f"OBS{index:06d}",
                "patient": patient,
                "date": when,
                "organization_id": org,
                "code": code,
                "display": display,
                "value": round(rng.uniform(3.0, 160.0), 2),
                "is_abnormal": rng.random() < 0.28,
                "status": "entered-in-error" if rng.random() < 0.04 else "final",
            }
        )
        if index % 7 == 0:
            drug_code, drug = DRUGS[index % len(DRUGS)]
            event_code, event = EVENTS[(index // 7) % len(EVENTS)]
            adverse.append(
                {
                    "sequence": f"{index:08d}",
                    "event_id": f"AE{index:06d}",
                    "patient": patient,
                    "date": when,
                    "organization_id": org,
                    "drug_code": drug_code,
                    "drug": drug,
                    "event_code": event_code,
                    "event": event,
                    "is_serious": rng.random() < 0.22,
                }
            )
        source_system, channel = SOURCES[index % len(SOURCES)]
        documents.append(
            {
                "sequence": f"{index:08d}",
                "document_id": f"DOC{index:06d}",
                "date": when,
                "organization_id": org,
                "source_system": source_system,
                "channel": channel,
                "document_type": rng.choice(["discharge-summary", "lab-report", "referral"]),
                "page_count": rng.randrange(1, 40),
                "chunk_count": rng.randrange(5, 120),
                "processing_ms": round(rng.lognormvariate(6.0, 0.8), 1),
                "used_ocr": rng.random() < 0.3,
                "quality_score": round(rng.uniform(0.55, 0.99), 3),
                "succeeded": rng.random() > 0.05,
            }
        )
        answers.append(
            {
                "sequence": f"{index:08d}",
                "answer_id": f"ANS{index:06d}",
                "date": when,
                "organization_id": org,
                "role": rng.choice(["physician", "pharmacist", "nurse", "analyst"]),
                "question_category": rng.choice(
                    ["medication", "labs", "history", "guideline", "administrative"]
                ),
                "response_mode": rng.choice(["answer", "uncertain", "clarification"]),
                "latency_ms": round(rng.lognormvariate(7.0, 0.5), 1),
                "citation_count": rng.randrange(0, 8),
                "is_grounded": rng.random() > 0.09,
                "was_abstained": rng.random() < 0.07,
            }
        )
        accesses.append(
            {
                "sequence": f"{index:08d}",
                "access_id": f"ACC{index:06d}",
                "date": when,
                "organization_id": org,
                "role": rng.choice(["physician", "pharmacist", "analyst"]),
                "is_service_account": rng.random() < 0.2,
                "purpose": rng.choice(["TREAT", "HOPERAT", "HRESCH", "BTG"]),
                "outcome": rng.choice(["permitted", "denied", "no_consent_on_file"]),
                "was_break_glass": rng.random() < 0.03,
                "was_reviewed": rng.random() < 0.6,
            }
        )
        if index % 30 == 0:
            jobs.append(
                {
                    "sequence": f"{index:08d}",
                    "run_id": f"JOB{index:06d}",
                    "date": when,
                    "organization_id": org,
                    "job_kind": rng.choice(["deidentification", "embedding", "export"]),
                    "status": "failed" if rng.random() < 0.15 else "succeeded",
                    "duration_ms": round(rng.uniform(500, 90000), 1),
                    "rows_processed": rng.randrange(100, 50000),
                    "rows_rejected": rng.randrange(0, 40),
                }
            )

    return {
        "encounters": encounters,
        "observations": observations,
        "adverse": adverse,
        "documents": documents,
        "answers": answers,
        "accesses": accesses,
        "jobs": jobs,
    }


def register_loaders(pipeline: Pipeline, builder: DimensionBuilder) -> None:
    """Wire every source to its fact, de-identifying on the way.

    Each transform is where the identifiers stop. Names never appear; medical record numbers
    become salted pseudonyms; exact birth dates become age bands; postal codes become at most
    three digits and nothing at all in a restricted area.
    """

    def cohort_key(patient: dict[str, Any], salt: str, when: dt.date) -> tuple[str, dict[str, Any]]:
        del salt  # the cohort key is derived from banded attributes, not from an identifier
        age = when.year - patient["birth_date"].year
        band = age_band(age)
        prefix = postal_prefix(patient["postal_code"], restricted=RESTRICTED_ZIPS)
        natural = f"{band}|{patient['sex']}|{prefix}"
        key = builder.key_for("dim_cohort", natural)
        return key, {
            "cohort_key": key,
            "age_band": band,
            "sex": patient["sex"],
            "postal_prefix": prefix,
            "risk_band": "unknown",
        }

    def org_key(organization_id: str) -> tuple[str, dict[str, Any]]:
        key = builder.key_for("dim_organization", organization_id)
        return key, {
            "organization_key": key,
            "organization_id": organization_id,
            "organization_name": organization_id.split(":", 1)[-1].replace("-", " ").title(),
            "organization_kind": "hospital",
            "region": "US-MA",
        }

    def code_key(code: str, display: str, category: str) -> tuple[str, dict[str, Any]]:
        key = builder.key_for("dim_code", f"{category}|{code}")
        return key, {
            "code_key": key,
            "code": code,
            "code_system": "http://loinc.org" if category == "analyte" else "urn:rxnorm",
            "display": display,
            "category": category,
        }

    def role_key(role: str, is_service: bool) -> tuple[str, dict[str, Any]]:
        key = builder.key_for("dim_actor_role", role)
        return key, {
            "role_key": key,
            "role": role,
            "is_service_account": is_service,
        }

    def source_key(system: str, channel: str) -> tuple[str, dict[str, Any]]:
        key = builder.key_for("dim_source_system", system)
        return key, {
            "source_key": key,
            "source_system": system,
            "channel": channel,
            "interface_kind": "hl7v2",
        }

    def encounter_transform(record: dict[str, Any], salt: str) -> dict[str, Any]:
        cohort, _ = cohort_key(record["patient"], salt, record["date"])
        organization, _ = org_key(record["organization_id"])
        return {
            "organization_id": record["organization_id"],
            "date_key": builder.date_key(record["date"]),
            "load_id": "",
            "encounter_key": pseudonym(record["encounter_id"], salt=salt, prefix="e"),
            "patient_key": pseudonym(record["patient"]["mrn"], salt=salt, prefix="p"),
            "cohort_key": cohort,
            "organization_key": organization,
            "encounter_class": record["encounter_class"],
            "length_of_stay_days": record["length_of_stay_days"],
            "is_readmission": record["is_readmission"],
        }

    def encounter_dimensions(record: dict[str, Any], salt: str) -> list[tuple[str, dict[str, Any]]]:
        _, cohort_row = cohort_key(record["patient"], salt, record["date"])
        _, org_row = org_key(record["organization_id"])
        return [
            ("dim_cohort", cohort_row),
            ("dim_organization", org_row),
            ("dim_date", builder.date_row(record["date"])),
        ]

    def observation_transform(record: dict[str, Any], salt: str) -> dict[str, Any]:
        cohort, _ = cohort_key(record["patient"], salt, record["date"])
        organization, _ = org_key(record["organization_id"])
        code, _ = code_key(record["code"], record["display"], "analyte")
        return {
            "organization_id": record["organization_id"],
            "date_key": builder.date_key(record["date"]),
            "load_id": "",
            "observation_key": pseudonym(record["observation_id"], salt=salt, prefix="o"),
            "patient_key": pseudonym(record["patient"]["mrn"], salt=salt, prefix="p"),
            "cohort_key": cohort,
            "code_key": code,
            "organization_key": organization,
            "value": record["value"],
            "unit": "mmol/L",
            "is_abnormal": record["is_abnormal"],
            "status": record["status"],
        }

    def observation_dimensions(
        record: dict[str, Any], salt: str
    ) -> list[tuple[str, dict[str, Any]]]:
        _, cohort_row = cohort_key(record["patient"], salt, record["date"])
        _, org_row = org_key(record["organization_id"])
        _, code_row = code_key(record["code"], record["display"], "analyte")
        return [
            ("dim_cohort", cohort_row),
            ("dim_organization", org_row),
            ("dim_code", code_row),
            ("dim_date", builder.date_row(record["date"])),
        ]

    def adverse_transform(record: dict[str, Any], salt: str) -> dict[str, Any]:
        cohort, _ = cohort_key(record["patient"], salt, record["date"])
        organization, _ = org_key(record["organization_id"])
        drug, _ = code_key(record["drug_code"], record["drug"], "drug")
        event, _ = code_key(record["event_code"], record["event"], "event")
        return {
            "organization_id": record["organization_id"],
            "date_key": builder.date_key(record["date"]),
            "load_id": "",
            "event_key": pseudonym(record["event_id"], salt=salt, prefix="a"),
            "patient_key": pseudonym(record["patient"]["mrn"], salt=salt, prefix="p"),
            "cohort_key": cohort,
            "drug_code_key": drug,
            "event_code_key": event,
            "organization_key": organization,
            "seriousness": "serious" if record["is_serious"] else "non-serious",
            "is_serious": record["is_serious"],
        }

    def adverse_dimensions(record: dict[str, Any], salt: str) -> list[tuple[str, dict[str, Any]]]:
        _, cohort_row = cohort_key(record["patient"], salt, record["date"])
        _, org_row = org_key(record["organization_id"])
        _, drug_row = code_key(record["drug_code"], record["drug"], "drug")
        _, event_row = code_key(record["event_code"], record["event"], "event")
        return [
            ("dim_cohort", cohort_row),
            ("dim_organization", org_row),
            ("dim_code", drug_row),
            ("dim_code", event_row),
            ("dim_date", builder.date_row(record["date"])),
        ]

    def document_transform(record: dict[str, Any], salt: str) -> dict[str, Any]:
        organization, _ = org_key(record["organization_id"])
        source, _ = source_key(record["source_system"], record["channel"])
        return {
            "organization_id": record["organization_id"],
            "date_key": builder.date_key(record["date"]),
            "load_id": "",
            "document_key": pseudonym(record["document_id"], salt=salt, prefix="d"),
            "source_key": source,
            "organization_key": organization,
            "document_type": record["document_type"],
            "page_count": record["page_count"],
            "chunk_count": record["chunk_count"],
            "processing_ms": record["processing_ms"],
            "used_ocr": record["used_ocr"],
            "quality_score": record["quality_score"],
            "succeeded": record["succeeded"],
        }

    def document_dimensions(record: dict[str, Any], salt: str) -> list[tuple[str, dict[str, Any]]]:
        del salt
        _, org_row = org_key(record["organization_id"])
        _, source_row = source_key(record["source_system"], record["channel"])
        return [
            ("dim_organization", org_row),
            ("dim_source_system", source_row),
            ("dim_date", builder.date_row(record["date"])),
        ]

    def answer_transform(record: dict[str, Any], salt: str) -> dict[str, Any]:
        organization, _ = org_key(record["organization_id"])
        role, _ = role_key(record["role"], False)
        return {
            "organization_id": record["organization_id"],
            "date_key": builder.date_key(record["date"]),
            "load_id": "",
            "answer_key": pseudonym(record["answer_id"], salt=salt, prefix="w"),
            "role_key": role,
            "organization_key": organization,
            "question_category": record["question_category"],
            "response_mode": record["response_mode"],
            "latency_ms": record["latency_ms"],
            "citation_count": record["citation_count"],
            "is_grounded": record["is_grounded"],
            "was_abstained": record["was_abstained"],
        }

    def answer_dimensions(record: dict[str, Any], salt: str) -> list[tuple[str, dict[str, Any]]]:
        del salt
        _, org_row = org_key(record["organization_id"])
        _, role_row = role_key(record["role"], False)
        return [
            ("dim_organization", org_row),
            ("dim_actor_role", role_row),
            ("dim_date", builder.date_row(record["date"])),
        ]

    def access_transform(record: dict[str, Any], salt: str) -> dict[str, Any]:
        organization, _ = org_key(record["organization_id"])
        role, _ = role_key(record["role"], record["is_service_account"])
        return {
            "organization_id": record["organization_id"],
            "date_key": builder.date_key(record["date"]),
            "load_id": "",
            "access_key": pseudonym(record["access_id"], salt=salt, prefix="x"),
            "role_key": role,
            "organization_key": organization,
            "purpose": record["purpose"],
            "outcome": record["outcome"],
            "was_break_glass": record["was_break_glass"],
            "was_reviewed": record["was_reviewed"],
        }

    def access_dimensions(record: dict[str, Any], salt: str) -> list[tuple[str, dict[str, Any]]]:
        del salt
        _, org_row = org_key(record["organization_id"])
        _, role_row = role_key(record["role"], record["is_service_account"])
        return [
            ("dim_organization", org_row),
            ("dim_actor_role", role_row),
            ("dim_date", builder.date_row(record["date"])),
        ]

    def job_transform(record: dict[str, Any], salt: str) -> dict[str, Any]:
        organization, _ = org_key(record["organization_id"])
        return {
            "organization_id": record["organization_id"],
            "date_key": builder.date_key(record["date"]),
            "load_id": "",
            "run_key": pseudonym(record["run_id"], salt=salt, prefix="j"),
            "organization_key": organization,
            "job_kind": record["job_kind"],
            "status": record["status"],
            "duration_ms": record["duration_ms"],
            "rows_processed": record["rows_processed"],
            "rows_rejected": record["rows_rejected"],
        }

    def job_dimensions(record: dict[str, Any], salt: str) -> list[tuple[str, dict[str, Any]]]:
        del salt
        _, org_row = org_key(record["organization_id"])
        return [
            ("dim_organization", org_row),
            ("dim_date", builder.date_row(record["date"])),
        ]

    for source, fact, key, transform, dimensions in (
        (
            "encounters",
            "fact_encounter",
            "encounter_key",
            encounter_transform,
            encounter_dimensions,
        ),
        (
            "observations",
            "fact_observation",
            "observation_key",
            observation_transform,
            observation_dimensions,
        ),
        ("adverse", "fact_adverse_event", "event_key", adverse_transform, adverse_dimensions),
        (
            "documents",
            "fact_document_ingestion",
            "document_key",
            document_transform,
            document_dimensions,
        ),
        ("answers", "fact_answer", "answer_key", answer_transform, answer_dimensions),
        ("accesses", "fact_phi_access", "access_key", access_transform, access_dimensions),
        ("jobs", "fact_job_run", "run_key", job_transform, job_dimensions),
    ):
        pipeline.register(
            TableLoader(
                source=source,
                fact=fact,
                natural_key=(key,),
                transform=transform,
                dimension_rows=dimensions,
            )
        )


def run_demo() -> None:
    tracemalloc.start()
    benches: list[Bench] = []
    print(f"{'=' * 78}\nANALYTICS WAREHOUSE — PHASE 7 VERIFICATION\n{'=' * 78}")

    schema = default_schema()
    warehouse = Warehouse(schema)
    metrics = load_metrics(CATALOGUE, schema)
    templates = build_templates(metrics)
    dashboards = DashboardRegistry(metrics)
    for board in default_dashboards():
        dashboards.register(board)

    print(f"\n{'-' * 78}\n1. MODEL\n{'-' * 78}")
    print(f"  facts        : {[f.name for f in schema.facts]}")
    print(f"  dimensions   : {[d.name for d in schema.dimensions]}")
    print(f"  metrics      : {metrics.count()} across {len(MetricCategory)} categories")
    print(f"  templates    : {len(templates.keys())}")
    print(f"  dashboards   : {list(dashboards.keys())}")

    # ---- 2. ETL --------------------------------------------------------------------------
    print(f"\n{'-' * 78}\n2. DE-IDENTIFYING ETL\n{'-' * 78}")
    builder = DimensionBuilder(salt=SALT)
    pipeline = Pipeline(warehouse, salt=SALT, watermarks=None)
    register_loaders(pipeline, builder)

    sources = generate_sources()
    started = time.perf_counter()
    total_loaded = 0
    for source, fact in (
        ("encounters", "fact_encounter"),
        ("observations", "fact_observation"),
        ("adverse", "fact_adverse_event"),
        ("documents", "fact_document_ingestion"),
        ("answers", "fact_answer"),
        ("accesses", "fact_phi_access"),
        ("jobs", "fact_job_run"),
    ):
        run = pipeline.run(
            source, fact, batched(sources[source], size=200, cursor_field="sequence"), at=NOW
        )
        total_loaded += run.rows_loaded
        print(f"  {run.render()}")
    benches.append(Bench("etl: load all facts", total_loaded, time.perf_counter() - started))

    print("\n  rerunning the same batches (idempotency):")
    rerun = pipeline.run(
        "observations",
        "fact_observation",
        batched(sources["observations"], size=200, cursor_field="sequence"),
        at=NOW,
    )
    print(f"  {rerun.render()}")

    print(f"\n  warehouse    : {warehouse.statistics()}")
    sample = next(warehouse.rows("fact_observation", ORG))
    print(f"  sample fact  : {sorted(sample)}")
    print(
        "  identifiers  : no name, no MRN, no exact date — "
        f"patient_key={sample['patient_key'][:12]}..., date_key={sample['date_key']}"
    )

    # ---- 3. queries ----------------------------------------------------------------------
    print(f"\n{'-' * 78}\n3. QUERIES\n{'-' * 78}")
    analyst = AnalyticsPrincipal(
        principal_id="user:analyst", organization_id=ORG, scopes=frozenset({ANALYTICS_READ.name})
    )
    officer = AnalyticsPrincipal(
        principal_id="user:compliance",
        organization_id=ORG,
        scopes=frozenset({ANALYTICS_READ.name, ANALYTICS_GOVERNANCE.name}),
    )
    executor = QueryExecutor(warehouse, metrics, templates)

    result = executor.execute(
        QueryRequest(
            template_key="adverse_event_trend",
            principal=analyst,
            parameters={"from": "2025-10-01", "to": "2026-03-31"},
            group_by=("dim_code.display",),
            at=NOW,
        )
    )
    print(result.render())

    rate = executor.execute(
        QueryRequest(
            template_key="grounding_rate",
            principal=analyst,
            parameters={"from": "2025-10-01", "to": "2026-03-31"},
            group_by=("question_category",),
            at=NOW,
        )
    )
    print()
    print(rate.render())

    # ---- 4. refusals ---------------------------------------------------------------------
    print(f"\n{'-' * 78}\n4. WHAT THE QUERY SURFACE REFUSES\n{'-' * 78}")
    attempts: list[tuple[str, dict[str, Any]]] = [
        (
            "governance metric without the governance scope",
            {"template_key": "phi_access_summary", "principal": analyst, "group_by": ("outcome",)},
        ),
        (
            "a parameter the template does not declare",
            {
                "template_key": "adverse_event_trend",
                "principal": analyst,
                "parameters": {"sql": "DROP TABLE fact_encounter"},
            },
        ),
        (
            "grouping by a dimension the template forbids",
            {
                "template_key": "adverse_event_trend",
                "principal": analyst,
                "group_by": ("dim_cohort.postal_prefix",),
            },
        ),
        (
            "a date range wider than the template permits",
            {
                "template_key": "adverse_event_trend",
                "principal": analyst,
                "parameters": {"from": "2000-01-01", "to": "2026-03-31"},
            },
        ),
        (
            "'to' before 'from'",
            {
                "template_key": "adverse_event_trend",
                "principal": analyst,
                "parameters": {"from": "2026-03-31", "to": "2025-10-01"},
            },
        ),
        (
            "a malformed date",
            {
                "template_key": "adverse_event_trend",
                "principal": analyst,
                "parameters": {"from": "last tuesday"},
            },
        ),
        (
            "too many grouping dimensions",
            {
                "template_key": "cohort_size",
                "principal": analyst,
                "group_by": ("dim_cohort.age_band", "dim_cohort.sex", "dim_date.month"),
            },
        ),
    ]
    for label, kwargs in attempts:
        try:
            executor.execute(QueryRequest(at=NOW, **kwargs))  # type: ignore[arg-type]
            print(f"  {label:<50} ALLOWED  <-- unexpected")
        except Exception as exc:
            print(f"  {label:<50} refused: {str(exc)[:70]}")

    # ---- 5. disclosure control -----------------------------------------------------------
    print(f"\n{'-' * 78}\n5. DISCLOSURE CONTROL\n{'-' * 78}")
    policy = DisclosurePolicy(minimum_cell_size=11)

    table = [
        Cell(group=("North",), value=42, subjects=42),
        Cell(group=("South",), value=38, subjects=38),
        Cell(group=("East",), value=7, subjects=7),
    ]
    naive_total = sum(c.value or 0 for c in table)
    outcome = apply_disclosure_control(list(table), policy=policy, total=naive_total)
    published = [(c.group[0], c.value) for c in outcome.cells if c.is_published]
    print(f"  input                 : {[(c.group[0], c.value) for c in table]} total={naive_total}")
    print(f"  primary suppressed    : {outcome.primary_suppressed}")
    print(f"  complementary         : {outcome.complementary_suppressed}")
    print(f"  total withheld        : {outcome.total_suppressed}")
    print(f"  published cells       : {published}")
    if outcome.total_suppressed:
        recoverable = None
    else:
        recoverable = (outcome.total or 0) - sum(v or 0 for _, v in published)
    print(
        "  subtraction attack    : "
        + (
            "blocked — the total is withheld"
            if recoverable is None
            else f"residual {recoverable} across {outcome.suppressed_count} suppressed cells"
        )
    )
    print(f"  note to the consumer  : {outcome.note()}")

    # ---- 6. dashboards -------------------------------------------------------------------
    print(f"\n{'-' * 78}\n6. DASHBOARDS\n{'-' * 78}")
    started = time.perf_counter()
    renders = 0
    for key in dashboards.keys():  # noqa: SIM118 - a registry, not a dict
        principal = officer if key == "governance" else analyst
        render = dashboards.render(
            key,
            executor,
            principal,
            parameters={"from": "2025-10-01", "to": "2026-03-31"},
            at=NOW,
        )
        renders += 1
        board = dashboards.get(key)
        assert board is not None
        print(
            f"  {board.title:<32} tiles={len(render.tiles):<2} "
            f"failed={len(render.failed_tiles)} stale={len(render.stale_tiles)}"
        )
        for tile in render.failed_tiles:
            print(f"      ! {tile.tile.title}: {tile.error[:80]}")
    benches.append(Bench("dashboard render", renders, time.perf_counter() - started))

    print(
        "\n"
        + dashboards.render(
            "governance",
            executor,
            officer,
            parameters={"from": "2025-10-01", "to": "2026-03-31"},
            at=NOW,
        ).render()[:1400]
    )

    # ---- 7. scheduled reports ------------------------------------------------------------
    print(f"\n{'-' * 78}\n7. SCHEDULED REPORTS\n{'-' * 78}")
    delivery = InMemoryDelivery()
    scheduler = ReportScheduler(dashboards, executor, delivery)
    scheduler.register(
        ReportDefinition(
            key="weekly-governance",
            title="Weekly governance summary",
            dashboard_key="governance",
            schedule=Schedule(kind=ScheduleKind.WEEKLY, hour_utc=6, day_of_week=0),
            principal=officer,
            formats=(ReportFormat.MARKDOWN, ReportFormat.CSV),
            recipients=("compliance@example.org",),
            parameters={"from": "2025-10-01", "to": "2026-03-31"},
        )
    )
    scheduler.register(
        ReportDefinition(
            key="monthly-pharmacovigilance",
            title="Monthly pharmacovigilance signal",
            dashboard_key="clinical-pharmacovigilance",
            schedule=Schedule(kind=ScheduleKind.MONTHLY, hour_utc=7, day_of_month=1),
            principal=analyst,
            formats=(ReportFormat.MARKDOWN,),
            recipients=("pv@example.org",),
            parameters={"from": "2025-10-01", "to": "2026-03-31"},
        )
    )
    due = scheduler.due(NOW)
    print(f"  due at {NOW:%Y-%m-%d %H:%M} UTC: {[r.key for r in due]}")
    for run in scheduler.run_due(NOW):
        print(f"  {run.report_key:<28} {run.status:<10} formats={sorted(run.artifacts)}")
    print(f"  delivered            : {len(delivery.delivered)}")
    print(f"  second pass (not due): {[r.key for r in scheduler.due(NOW)]}")

    markdown = delivery.for_report("weekly-governance")[0][2]["text/markdown"]
    print("\n  --- report extract ---")
    print("\n".join("  " + line for line in markdown.splitlines()[:16]))

    # ---- 8. tenant isolation -------------------------------------------------------------
    print(f"\n{'-' * 78}\n8. TENANT ISOLATION\n{'-' * 78}")
    other = AnalyticsPrincipal(
        principal_id="user:other",
        organization_id=OTHER_ORG,
        scopes=frozenset({ANALYTICS_READ.name}),
    )
    mine = executor.execute(
        QueryRequest(
            "adverse_event_trend", analyst, {"from": "2025-10-01", "to": "2026-03-31"}, at=NOW
        )
    )
    theirs = executor.execute(
        QueryRequest(
            "adverse_event_trend", other, {"from": "2025-10-01", "to": "2026-03-31"}, at=NOW
        )
    )
    print(f"  {ORG:<22} scanned {mine.rows_scanned} rows, total {mine.suppression.total}")
    print(f"  {OTHER_ORG:<22} scanned {theirs.rows_scanned} rows, total {theirs.suppression.total}")
    print(f"  disjoint scans        : {mine.rows_scanned != theirs.rows_scanned}")

    # ---- 9. API --------------------------------------------------------------------------
    print(f"\n{'-' * 78}\n9. API\n{'-' * 78}")
    api = AnalyticsApi(
        executor=executor,
        metrics=metrics,
        templates=templates,
        dashboards=dashboards,
        scheduler=scheduler,
    )
    print(f"  health                 : {api.health().body['status']}")
    ok = api.get_metric(
        "adverse_event_count",
        analyst,
        parameters={"from": "2025-10-01", "to": "2026-03-31"},
        group_by=("dim_date.month",),
        at=NOW,
    )
    print(
        f"  GET adverse_event_count: {ok.status} rows={len(ok.body['rows'])} "
        f"as-of={ok.headers.get('X-Data-As-Of', '')[:19]}"
    )
    print(f"  GET unknown metric     : {api.get_metric('made_up', analyst, at=NOW).status}")
    forbidden = api.get_metric("phi_access_count", analyst, group_by=("outcome",), at=NOW)
    print(f"  GET governance as analyst: {forbidden.status} {forbidden.body['detail'][:60]}")
    print(f"  GET /analytics/reports : {len(api.list_reports(officer).body['reports'])} reports")
    print(f"  catalogue              : {api.list_metrics(analyst).body['count']} metrics visible")

    # ---- 10. benchmarks ------------------------------------------------------------------
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    print(f"\n{'-' * 78}\n10. BENCHMARKS\n{'-' * 78}")
    iterations = 300
    started = time.perf_counter()
    for _ in range(iterations):
        executor.execute(
            QueryRequest(
                "adverse_event_trend",
                analyst,
                {"from": "2025-10-01", "to": "2026-03-31"},
                group_by=("dim_code.display",),
                at=NOW,
            )
        )
    benches.append(
        Bench("query: count grouped by dimension", iterations, time.perf_counter() - started)
    )

    started = time.perf_counter()
    for _ in range(iterations):
        executor.execute(
            QueryRequest(
                "grounding_rate",
                analyst,
                {"from": "2025-10-01", "to": "2026-03-31"},
                group_by=("dim_date.month",),
                at=NOW,
            )
        )
    benches.append(
        Bench("query: ratio grouped by month", iterations, time.perf_counter() - started)
    )

    started = time.perf_counter()
    for _ in range(iterations):
        executor.execute(
            QueryRequest(
                "ingestion_latency",
                analyst,
                {"from": "2025-10-01", "to": "2026-03-31"},
                group_by=("dim_source_system.source_system",),
                at=NOW,
            )
        )
    benches.append(Bench("query: p95 grouped by source", iterations, time.perf_counter() - started))

    cells = [Cell(group=(f"g{i}",), value=i, subjects=i) for i in range(1, 60)]
    started = time.perf_counter()
    for _ in range(iterations * 10):
        apply_disclosure_control(list(cells), policy=policy, total=sum(range(1, 60)))
    benches.append(
        Bench("disclosure control (59 cells)", iterations * 10, time.perf_counter() - started)
    )

    started = time.perf_counter()
    for _ in range(20):
        dashboards.render(
            "operational",
            executor,
            analyst,
            parameters={"from": "2025-10-01", "to": "2026-03-31"},
            at=NOW,
        )
    benches.append(Bench("dashboard render (5 tiles)", 20, time.perf_counter() - started))

    for bench in benches:
        print(bench.render())
    print(f"\n  {'peak memory (load + queries)':<46} {peak / 1024 / 1024:>12.2f} MB")
    print(
        "\n  In-process, no database. These are the costs of grouping, aggregation, and\n"
        "  disclosure control; a warehouse deployment's I/O is not represented and will\n"
        "  dominate at scale."
    )


def main() -> None:
    run_demo()


if __name__ == "__main__":  # pragma: no cover
    main()
