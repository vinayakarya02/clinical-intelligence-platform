"""End-to-end demonstration, load simulation, and benchmarks.

Builds a three-organisation ecosystem, feeds real HL7 through it, resolves identity across the
boundary, enforces consent, streams events, runs population analytics, exports a de-identified
dataset, drives a cross-organisation referral, and benchmarks each subsystem.

``python -m cip_interop.demo`` reproduces the Phase 6 verification run.

**Nothing here has exchanged a message with a real hospital system.** Every conformance claim is
against a specification document, not against a counterparty.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import pathlib
import time
import tracemalloc
from dataclasses import dataclass
from typing import Any

from cip_interop.api import ApiRequest, ClinicalApi
from cip_interop.consent import (
    ConsentEngine,
    ConsentRecord,
    Decision,
    DisclosureRequest,
    InMemoryAuditSink,
    Provision,
    RegionalPolicy,
)
from cip_interop.dashboards import Audience, DashboardRegistry
from cip_interop.datalake import (
    SAFE_HARBOR_CATEGORIES,
    DeidentificationMethod,
    ExportManifest,
    FeatureDefinition,
    FeatureStore,
    LakeLayer,
    deidentify,
)
from cip_interop.domain import PurposeOfUse
from cip_interop.empi.index import EmpiIndex
from cip_interop.fhir.definitions import FhirVersion
from cip_interop.fhir.repository import RepositoryRegistry
from cip_interop.fhir.resources import Resource
from cip_interop.imaging import (
    DicomSeries,
    DicomStudy,
    ImagingRegistry,
    Modality,
    PacsEndpoint,
    WorklistItem,
    to_imaging_study,
)
from cip_interop.mapping.engine import MappingEngine, load_mapping_set
from cip_interop.orgs import (
    AgreementRegistry,
    Organization,
    OrganizationContext,
    OrganizationDirectory,
    OrganizationKind,
    SharingAgreement,
)
from cip_interop.population import (
    CohortDefinition,
    PopulationAnalytics,
    QualityMeasure,
)
from cip_interop.routing import Channel, DeadLetterQueue, IntegrationEngine
from cip_interop.security import (
    AbacPolicy,
    AbacRule,
    ScimProvisioner,
    ScimUser,
    ScopeSet,
    StaticTokenVerifier,
    TokenClaims,
)
from cip_interop.streaming import ClinicalEventType, ConsumerGroup, EventStream
from cip_interop.workflow import TaskState, WorkflowKind, WorkflowOrchestrator

MAPS = pathlib.Path(__file__).parent / "mapping" / "maps" / "default-v2-r4.yaml"
TODAY = dt.date(2026, 3, 20)
NOW = dt.datetime(2026, 3, 20, 14, 30, tzinfo=dt.UTC)

#: Surname and given-name variety for the load simulation. Real populations have diverse
#: surnames; a generator that names everyone "Patient00001" makes every prefix-based blocking
#: key degenerate and measures a pathology rather than a throughput.
LOAD_SURNAMES = [
    "Alvarez",
    "Bennett",
    "Castillo",
    "Dubois",
    "Eriksen",
    "Fitzgerald",
    "Grimaldi",
    "Haruna",
    "Ibrahim",
    "Jankowski",
    "Kowalczyk",
    "Lindqvist",
    "Mbeki",
    "Nakamura",
    "Okonkwo",
    "Petrov",
    "Quintero",
    "Rasmussen",
    "Silva",
    "Thibault",
    "Ueda",
    "Vasquez",
    "Whitfield",
    "Xiong",
    "Yamamoto",
    "Zoric",
]
LOAD_GIVENS = [
    "Anna",
    "Bo",
    "Chen",
    "Dilip",
    "Elena",
    "Farid",
    "Grace",
    "Hugo",
    "Ines",
    "Jonas",
    "Kira",
    "Luca",
]

MERCY = "org:mercy-general"
MERCY_LAB = "org:mercy-lab"
ELSEWHERE = "org:st-elsewhere"
IMAGING = "org:valley-imaging"


@dataclass
class Bench:
    name: str
    operations: int
    seconds: float

    def render(self) -> str:
        per = (self.seconds / self.operations) * 1_000_000 if self.operations else 0.0
        rate = self.operations / self.seconds if self.seconds else 0.0
        return f"  {self.name:<44} {rate:>12,.0f} op/s {per:>10.2f} us each"


def build_directory() -> tuple[OrganizationDirectory, AgreementRegistry]:
    directory = OrganizationDirectory()
    directory.register(
        Organization(MERCY, "Mercy General Hospital", OrganizationKind.HOSPITAL, region="US-MA")
    )
    directory.register(
        Organization(MERCY_LAB, "Mercy Laboratory", OrganizationKind.LABORATORY, parent_id=MERCY)
    )
    directory.register(
        Organization(ELSEWHERE, "St Elsewhere Clinic", OrganizationKind.CLINIC, region="US-MA")
    )
    directory.register(
        Organization(IMAGING, "Valley Imaging", OrganizationKind.IMAGING_CENTER, region="US-MA")
    )

    agreements = AgreementRegistry(directory)
    agreements.register(
        SharingAgreement(
            agreement_id="agr-mercy-elsewhere",
            source_organization_id=MERCY,
            target_organization_id=ELSEWHERE,
            purposes=frozenset({PurposeOfUse.TREATMENT, PurposeOfUse.OPERATIONS}),
            effective_from=dt.date(2025, 1, 1),
            effective_until=dt.date(2027, 12, 31),
            signed_by="Chief Medical Information Officer",
        )
    )
    agreements.register(
        SharingAgreement(
            agreement_id="agr-imaging-mercy",
            source_organization_id=IMAGING,
            target_organization_id=MERCY,
            purposes=frozenset({PurposeOfUse.TREATMENT}),
            effective_from=dt.date(2025, 6, 1),
            signed_by="Radiology Director",
        )
    )
    # Deliberately expired, to exercise the refusal path.
    agreements.register(
        SharingAgreement(
            agreement_id="agr-elsewhere-imaging-expired",
            source_organization_id=ELSEWHERE,
            target_organization_id=IMAGING,
            purposes=frozenset({PurposeOfUse.TREATMENT}),
            effective_from=dt.date(2023, 1, 1),
            effective_until=dt.date(2025, 6, 30),
            signed_by="Practice Manager",
        )
    )
    return directory, agreements


def segment(name: str, **fields: str) -> str:
    """Build a segment from field *numbers*, so nobody counts pipes.

    Counting pipes by hand is how a visit number ends up in PV1-18 and the encounter silently
    stops being built — which is exactly what the first run of this demo did.
    """
    numbers = {int(k.lstrip("f")): v for k, v in fields.items()}
    highest = max(numbers)
    return "|".join([name, *(numbers.get(i, "") for i in range(1, highest + 1))])


def hl7_messages() -> list[tuple[str, str]]:
    """Realistic traffic. Each is ``(channel, raw)``."""
    return [
        (
            "mercy-adt",
            "MSH|^~\\&|ADT|MERCY|CIP|CIPFAC|20260320080000||ADT^A01|MRC0001|P|2.5.1\r"
            "EVN|A01|20260320080000\r"
            "PID|1||MRN7781^^^MERCY^MR~123-45-6789^^^SSA^SS||M\\T\\uller^Anna^Marie||19551103|F|||"
            "12 Elm St^^Boston^MA^02134^USA||6175550142\r"
            + segment("PV1", f1="1", f2="I", f3="ICU^12^A", f19="V44120", f44="20260320080000")
            + "\r",
        ),
        (
            "mercy-lab",
            "MSH|^~\\&|LAB|MERCYLAB|CIP|CIPFAC|20260320113000||ORU^R01|LAB0001|P|2.5.1\r"
            "PID|1||MRN7781^^^MERCY^MR||Muller^Anna||19551103|F\r"
            "OBR|1|PL5501|FL9901|CMP^Comprehensive metabolic panel^LN|||20260320110000|||||||"
            "||||||||20260320113000-0500|||F\r"
            "OBX|1|NM|2951-2^Sodium^LN||139|mmol/L|136-145|N|||F|||20260320110000\r"
            "OBX|2|NM|2823-3^Potassium^LN||5.4|mmol/L|3.5-5.1|H|||F|||20260320110000\r"
            "OBX|3|NM|2160-0^Creatinine^LN||1.4|mg/dL|0.6-1.1|H|||F|||20260320110000\r"
            "ZLB|1|internal lab batch 44\r",
        ),
        (
            "elsewhere-adt",
            "MSH|^~\\&|REG|STELSEWHERE|CIP|CIPFAC|20260318093000||ADT^A04|STE0001|P|2.5.1\r"
            "EVN|A04|20260318093000\r"
            "PID|1||STE44821^^^STELSEWHERE^MR~123-45-6789^^^SSA^SS||Muller^Anna||19551103|F|||"
            "12 Elm Street^^Boston^MA^02134-1122^USA||617-555-0142\r"
            + segment("PV1", f1="1", f2="O", f3="CLINIC", f19="E88120")
            + "\r",
        ),
        (
            "mercy-orders",
            "MSH|^~\\&|CPOE|MERCY|CIP|CIPFAC|20260320120000||ORM^O01|ORD0001|P|2.5.1\r"
            "PID|1||MRN7781^^^MERCY^MR||Muller^Anna||19551103|F\r"
            "ORC|NW|PLC9001||||||||||||20260320120000|||||||||||||S\r"
            "OBR|1|PLC9001||24627-2^Chest CT^LN\r",
        ),
        (
            "mercy-adt",
            "MSH|^~\\&|ADT|MERCY|CIP|CIPFAC|20260322160000||ADT^A03|MRC0002|P|2.5.1\r"
            "EVN|A03|20260322160000\r"
            "PID|1||MRN7781^^^MERCY^MR||Muller^Anna||19551103|F\r"
            + segment(
                "PV1",
                f1="1",
                f2="I",
                f3="ICU^12^A",
                f19="V44120",
                f44="20260320080000",
                f45="20260322160000",
            )
            + "\r",
        ),
    ]


def bad_messages() -> list[tuple[str, str, str]]:
    """Traffic that must be refused, with what each is testing."""
    return [
        (
            "mercy-adt",
            "not an HL7 message at all",
            "unparseable",
        ),
        (
            "mercy-adt",
            "MSH|^~\\&|ADT|MERCY|CIP|CIPFAC|20260320||ADT^A01|MRC9001|T|2.5.1\r"
            "EVN|A01|20260320\rPID|1||X1^^^MERCY^MR||Test^Patient||19700101|M\rPV1|1|I",
            "training data flagged T in MSH-11",
        ),
        (
            "mercy-adt",
            "MSH|^~\\&|ADT|MERCY|CIP|CIPFAC|20260320||ADT^A40|MRC9002|P|2.5.1\r"
            "EVN|A40|20260320\rPID|1||X2^^^MERCY^MR||Merge^Target||19700101|M",
            "A40 merge with no MRG segment",
        ),
        (
            "mercy-adt",
            "MSH|^~\\&|ADT|MERCY|CIP|CIPFAC|20260320||ADT^A01|MRC0001|P|2.5.1\r"
            "EVN|A01|20260320\rPID|1||MRN7781^^^MERCY^MR||Muller^Anna||19551103|F\rPV1|1|I",
            "duplicate control id (retransmission)",
        ),
    ]


async def run_demo() -> None:
    tracemalloc.start()
    benches: list[Bench] = []
    print(f"{'=' * 78}\nCLINICAL ECOSYSTEM INTEROPERABILITY - PHASE 6 VERIFICATION\n{'=' * 78}")

    # ---- 1. the ecosystem -----------------------------------------------------------------
    directory, agreements = build_directory()
    mapping_set = load_mapping_set(MAPS)
    engine_mapping = MappingEngine(mapping_set)
    empi = EmpiIndex()
    repositories = RepositoryRegistry(fhir_version=FhirVersion.R4)
    stream = EventStream(partitions=8)
    dead_letters = DeadLetterQueue()
    integration = IntegrationEngine(
        empi=empi, repositories=repositories, stream=stream, dead_letters=dead_letters
    )

    for name, organization, source in (
        ("mercy-adt", MERCY, "MERCY-ADT"),
        ("mercy-lab", MERCY_LAB, "MERCY-LAB"),
        ("mercy-orders", MERCY, "MERCY-CPOE"),
        ("elsewhere-adt", ELSEWHERE, "STE-REG"),
    ):
        integration.register(
            Channel(
                name=name,
                source_system=source,
                organization_id=organization,
                mapping=engine_mapping,
            )
        )

    print(f"\n{'-' * 78}\n1. ECOSYSTEM\n{'-' * 78}")
    print(f"organisations : {directory.statistics()}")
    print(f"agreements    : {len(agreements.active_agreements(TODAY))} active on {TODAY}")
    print(f"mapping set   : {mapping_set.key()}, {len(mapping_set.consumed_fields())} fields read")
    print(f"channels      : {[c.name for c in integration.channels()]}")

    # ---- 2. ingestion ---------------------------------------------------------------------
    print(f"\n{'-' * 78}\n2. HL7 INGESTION\n{'-' * 78}")
    started = time.perf_counter()
    outcomes = []
    for channel_name, raw in hl7_messages():
        outcome = await integration.ingest(raw, channel_name=channel_name, at=NOW)
        outcomes.append(outcome)
        print(
            f"  {channel_name:<15} {outcome.control_id:<9} {outcome.ack_code} "
            f"resources={len(outcome.resources):<2} person={outcome.person_id[:20]:<20} "
            f"published={outcome.published}"
        )
        for warning in outcome.warnings[:1]:
            print(f"      ! {warning[:96]}")
    traced_ingest = Bench(
        "hl7 ingest (traced, see note)", len(outcomes), time.perf_counter() - started
    )

    print("\n  refusals:")
    for channel_name, raw, what in bad_messages():
        outcome = await integration.ingest(raw, channel_name=channel_name, at=NOW)
        print(f"    {what:<44} -> {outcome.ack_code} accepted={outcome.accepted}")

    # ---- 3. identity across organisations -------------------------------------------------
    print(f"\n{'-' * 78}\n3. IDENTITY RESOLUTION\n{'-' * 78}")
    person_id = next(o.person_id for o in outcomes if o.person_id)
    print(f"  resolved person      : {person_id}")
    print(f"  source records       : {[r.record_id for r in empi.records_for(person_id)]}")
    print(f"  holding organisations: {list(empi.organizations_for(person_id))}")
    print(f"  statistics           : {empi.statistics()}")
    for review in empi.open_reviews()[:2]:
        print(f"  review queued        : score {review.score.total:+.2f} for {review.record_id}")

    # ---- 4. consent -----------------------------------------------------------------------
    print(f"\n{'-' * 78}\n4. CONSENT AT DISCLOSURE\n{'-' * 78}")
    audit = InMemoryAuditSink()
    consent = ConsentEngine(
        audit_sink=audit,
        regional_policies={
            ELSEWHERE: RegionalPolicy(
                region="US-MA",
                forbidden_purposes=frozenset({PurposeOfUse.MARKETING}),
                categories_requiring_explicit_consent=frozenset({"SubstanceUseDisorder"}),
            )
        },
    )
    consent.file(
        ConsentRecord(
            consent_id="consent-001",
            person_id=person_id,
            organization_id=MERCY,
            base_decision=Decision.DENY,
            provisions=(
                Provision(
                    decision=Decision.PERMIT,
                    purposes=frozenset({PurposeOfUse.TREATMENT}),
                    note="treatment at any care-delivery organisation",
                ),
                Provision(
                    decision=Decision.DENY,
                    purposes=frozenset({PurposeOfUse.RESEARCH}),
                    note="patient declined research use",
                ),
            ),
            effective_from=dt.date(2025, 3, 1),
            recorded_by="registration:desk-2",
        )
    )

    clinician = OrganizationContext(
        principal_id="practitioner:jkim", organization_id=MERCY, roles=frozenset({"physician"})
    )
    service = OrganizationContext(
        principal_id="svc:etl", organization_id=MERCY, is_named_human=False
    )

    for label, request in (
        (
            "treatment, named clinician",
            DisclosureRequest(
                person_id, clinician, PurposeOfUse.TREATMENT, holding_organization_id=MERCY, at=NOW
            ),
        ),
        (
            "research, named clinician",
            DisclosureRequest(
                person_id, clinician, PurposeOfUse.RESEARCH, holding_organization_id=MERCY, at=NOW
            ),
        ),
        (
            "unknown patient",
            DisclosureRequest(
                "person:nobody",
                clinician,
                PurposeOfUse.TREATMENT,
                holding_organization_id=MERCY,
                at=NOW,
            ),
        ),
        (
            "break-glass, service account",
            DisclosureRequest(
                person_id,
                service,
                PurposeOfUse.BREAK_GLASS,
                holding_organization_id=MERCY,
                break_glass_reason="batch",
                at=NOW,
            ),
        ),
        (
            "break-glass, no reason",
            DisclosureRequest(
                person_id,
                clinician,
                PurposeOfUse.BREAK_GLASS,
                holding_organization_id=MERCY,
                at=NOW,
            ),
        ),
        (
            "break-glass, stated reason",
            DisclosureRequest(
                person_id,
                clinician,
                PurposeOfUse.BREAK_GLASS,
                holding_organization_id=MERCY,
                break_glass_reason="unresponsive in ED",
                at=NOW,
            ),
        ),
    ):
        evaluation = consent.evaluate(request)
        print(f"  {label:<30} -> {evaluation.outcome.value:<20} {evaluation.reason[:52]}")
    print(
        f"\n  audit entries: {len(audit.entries)} | break-glass: {len(audit.break_glass_entries())}"
    )

    started = time.perf_counter()
    for _ in range(2000):
        consent.evaluate(
            DisclosureRequest(
                person_id, clinician, PurposeOfUse.TREATMENT, holding_organization_id=MERCY, at=NOW
            )
        )
    traced_consent = Bench("consent evaluation (traced)", 2000, time.perf_counter() - started)

    # ---- 5. streaming ---------------------------------------------------------------------
    print(f"\n{'-' * 78}\n5. EVENT STREAM\n{'-' * 78}")
    consumer = ConsumerGroup("clinical-view", stream)
    delivered = 0
    for partition in range(stream.partition_count):
        for record in consumer.poll(partition):
            if consumer.consume(record):
                delivered += 1
            consumer.commit(partition, record.offset + 1)
    print(
        f"  records          : {stream.total_records()} across {stream.partition_count} partitions"
    )
    print(f"  consumed         : {delivered} | lag now: {consumer.total_lag()}")
    print(f"  patient ordering : {[str(r.event_type) for r in stream.read_key(person_id)]}")

    # Redelivery: the ledger must absorb it.
    from cip_interop.streaming import partition_for

    patient_partition = partition_for(person_id, stream.partition_count)
    replayed = duplicates = 0
    consumer.seek(patient_partition, 0)
    for record in consumer.poll(patient_partition):
        replayed += 1
        if not consumer.consume(record):
            duplicates += 1
    print(f"  replay           : {replayed} redelivered, {duplicates} suppressed as duplicates")
    print(f"  ordering issues  : {len(stream.ordering_violations())}")

    # Out-of-order arrival, deliberately.
    stream.publish(
        ClinicalEventType.LAB_RESULT_COMPLETED,
        partition_key=person_id,
        source_system="MERCY-LAB",
        source_sequence=99,
        organization_id=MERCY_LAB,
    )
    print(f"  after a sequence jump: {[v.render() for v in stream.ordering_violations()]}")

    # ---- 6. imaging -----------------------------------------------------------------------
    print(f"\n{'-' * 78}\n6. IMAGING\n{'-' * 78}")
    pacs = PacsEndpoint(
        name="valley-pacs",
        wado_rs_base="https://pacs.valley.example/dicomweb",
        organization_id=IMAGING,
    )
    imaging = ImagingRegistry()
    imaging.schedule(
        WorklistItem(
            accession_number="ACC55010",
            patient_reference=f"Patient/{person_id}",
            modality=Modality.CT,
            scheduled_at=NOW,
            organization_id=IMAGING,
            procedure_description="CT chest with contrast",
            service_request_id="sr-PLC9001",
        )
    )
    study = DicomStudy(
        study_instance_uid="1.2.840.113619.2.55.3.604688.1234.20260320.1",
        patient_reference=f"Patient/{person_id}",
        organization_id=IMAGING,
        accession_number="ACC55010",
        started=NOW,
        endpoint=pacs,
        based_on_order="sr-PLC9001",
        series=(
            DicomSeries(
                "1.2.840.113619.2.55.3.604688.1234.20260320.1.1",
                Modality.CT,
                series_number=1,
                description="Axial",
                instances=(),
            ),
            DicomSeries(
                "1.2.840.113619.2.55.3.604688.1234.20260320.1.2",
                Modality.CT,
                series_number=2,
                description="Coronal",
                instances=(),
            ),
        ),
    )
    reconciled = imaging.register_study(study)
    orphan = DicomStudy(
        study_instance_uid="1.2.840.113619.2.55.3.604688.9999.20260320.7",
        patient_reference="Patient/typed-at-console",
        organization_id=IMAGING,
        accession_number="ACC00000",
    )
    imaging.register_study(orphan)
    print(f"  reconciled against worklist : {reconciled}")
    print(f"  unreconciled                : {[reason for _, reason in imaging.unreconciled]}")
    print(f"  retrieval url               : {study.retrieve_url()}")
    imaging_resource = to_imaging_study(study)
    from cip_interop.fhir.validation import validate_resource

    print(f"  ImagingStudy valid          : {validate_resource(imaging_resource).valid}")
    print(f"  statistics                  : {imaging.statistics()}")

    # ---- 7. population health -------------------------------------------------------------
    print(f"\n{'-' * 78}\n7. POPULATION HEALTH\n{'-' * 78}")
    mercy_repository = repositories.for_organization(MERCY)
    _seed_population(mercy_repository)
    analytics = PopulationAnalytics(mercy_repository, min_cell_size=5)

    cohort = analytics.resolve(
        CohortDefinition(
            cohort_id="ckd-on-raas",
            name="Chronic kidney disease",
            inclusion=lambda resources: any(
                r.resource_type == "Condition"
                and any(
                    c.get("code") == "N18.3" for c in (r.data.get("code") or {}).get("coding", [])
                )
                for r in resources
            ),
        )
    )
    print(f"  cohort         : {cohort.to_json(min_cell_size=5)}")
    print(f"  prevalence     : {analytics.prevalence('N18.3')}")
    segmentation = analytics.segment()
    print(f"  risk bands     : {segmentation.to_json(min_cell_size=5)}")

    measure = QualityMeasure(
        measure_id="CIP-CKD-01",
        title="eGFR monitored in chronic kidney disease",
        initial_population=lambda rs: any(r.resource_type == "Patient" for r in rs),
        denominator=lambda rs: any(
            r.resource_type == "Condition"
            and any(c.get("code") == "N18.3" for c in (r.data.get("code") or {}).get("coding", []))
            for r in rs
        ),
        numerator=lambda rs: any(
            r.resource_type == "Observation"
            and any(
                c.get("code") == "33914-3" for c in (r.data.get("code") or {}).get("coding", [])
            )
            for r in rs
        ),
        denominator_exclusion=lambda rs: any(
            r.resource_type == "Condition"
            and any(c.get("code") == "Z99.2" for c in (r.data.get("code") or {}).get("coding", []))
            for r in rs
        ),
        denominator_exception=lambda rs: any(
            r.resource_type == "Condition"
            and any(c.get("code") == "Z51.5" for c in (r.data.get("code") or {}).get("coding", []))
            for r in rs
        ),
    )
    result = analytics.compute(measure)
    print(f"  quality measure: {result.render()}")

    # ---- 8. data lake ---------------------------------------------------------------------
    print(f"\n{'-' * 78}\n8. DATA LAKE EXPORT\n{'-' * 78}")
    patients = mercy_repository.search("Patient", count=1000).resources
    deidentified = [deidentify(p, salt="extract-2026-03-salt", age_years=71) for p in patients[:3]]
    for entry in deidentified[:2]:
        print(f"  {entry.render()}")
    sample = deidentified[0].resource
    print(f"  sample fields  : {sorted(sample.data)}")
    manifest = ExportManifest(
        export_id="exp-0001",
        layer=LakeLayer.GOLD,
        method=DeidentificationMethod.SAFE_HARBOR,
        ruleset_version="1.0.0",
        resource_counts={"Patient": len(deidentified)},
        organization_id=MERCY,
        categories_acted_on=SAFE_HARBOR_CATEGORIES,
        salted=True,
    )
    print(
        f"  manifest       : method={manifest.to_json()['deidentification']['method']}, "
        f"expert_determination={manifest.to_json()['deidentification']['expert_determination']}, "
        f"categories={len(manifest.categories_acted_on)}"
    )

    features = FeatureStore()
    features.define(
        FeatureDefinition(
            name="latest_potassium",
            description="Most recent potassium",
            dtype="float",
            source="Observation 2823-3",
        )
    )
    features.write("p-1", "latest_potassium", 4.1, known_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC))
    features.write("p-1", "latest_potassium", 5.4, known_at=dt.datetime(2026, 3, 20, tzinfo=dt.UTC))
    february = dt.datetime(2026, 2, 1, tzinfo=dt.UTC)
    print(
        "  point-in-time  : as of 2026-02-01 -> "
        f"{features.read_at('p-1', 'latest_potassium', as_of=february)}, "
        f"as of now -> {features.read_at('p-1', 'latest_potassium', as_of=NOW)}"
    )

    # ---- 9. security and API --------------------------------------------------------------
    print(f"\n{'-' * 78}\n9. SECURITY AND CLINICAL API\n{'-' * 78}")
    scim = ScimProvisioner()
    scim.create(
        ScimUser(
            scim_id="u-jkim",
            user_name="jkim",
            display_name="J Kim",
            organization_id=MERCY,
            roles=frozenset({"physician"}),
        )
    )
    verifier = StaticTokenVerifier(
        expected_issuer="https://idp.mercy.example", expected_audience="cip-fhir"
    )
    claims = TokenClaims(
        subject="u-jkim",
        issuer="https://idp.mercy.example",
        audience="cip-fhir",
        scopes=ScopeSet.parse(
            "openid fhirUser user/Patient.rs user/Observation.rs?category=laboratory"
        ),
        expires_at=NOW + dt.timedelta(hours=1),
        issued_at=NOW - dt.timedelta(minutes=5),
        organization_id=MERCY,
    )
    verifier.register("tok-jkim", claims)
    print(f"  token verified : {verifier.verify('tok-jkim', at=NOW).subject}")
    print(f"  parsed scopes  : {[s.render() for s in claims.scopes.scopes]}")

    policy = (
        AbacPolicy()
        .add(
            AbacRule(
                rule_id="permit-clinician-treatment",
                effect="permit",
                subject_attributes={"role:physician": "true"},
                purposes=frozenset({PurposeOfUse.TREATMENT, PurposeOfUse.OPERATIONS}),
                description="clinicians may read for treatment",
            )
        )
        .add(
            AbacRule(
                rule_id="permit-service-operations",
                effect="permit",
                subject_attributes={"service_account": "true"},
                purposes=frozenset({PurposeOfUse.OPERATIONS, PurposeOfUse.RESEARCH}),
                description="analytics service accounts may run population exports",
            )
        )
        .add(
            AbacRule(
                rule_id="deny-marketing",
                effect="deny",
                purposes=frozenset({PurposeOfUse.MARKETING}),
                description="marketing use is never permitted",
            )
        )
    )
    api = ClinicalApi(
        repositories=repositories,
        consent=consent,
        agreements=agreements,
        # Consent is filed against the person; FHIR ids are organisation-local. The EMPI is
        # what joins them, so the API is given the resolver rather than assuming they are the
        # same identifier.
        resolve_person=empi.person_for_resource,
        policy=policy,
        base_url="https://cip.example",
    )
    request = ApiRequest(context=clinician, claims=claims, purpose=PurposeOfUse.TREATMENT, at=NOW)
    # The patient the HL7 traffic created, not a seeded one — so the read exercises the
    # organisation-local id to person resolution that consent depends on.
    patient_resource = mercy_repository.read("Patient", "pat-MRN7781").resource
    response = api.read("Patient", patient_resource.id, request, organization_id=MERCY)
    print(f"  GET Patient    : {response.status} etag={response.headers.get('ETag')}")

    marketing = ApiRequest(context=clinician, claims=claims, purpose=PurposeOfUse.MARKETING, at=NOW)
    marketing_response = api.read("Patient", patient_resource.id, marketing, organization_id=MERCY)
    print(f"  marketing read : {marketing_response.status}")

    cross = OrganizationContext(principal_id="practitioner:lee", organization_id=IMAGING)
    cross_request = ApiRequest(context=cross, claims=claims, purpose=PurposeOfUse.TREATMENT, at=NOW)
    cross_response = api.read("Patient", patient_resource.id, cross_request, organization_id=MERCY)
    print(
        f"  cross-org read : {cross_response.status} "
        f"{cross_response.body['issue'][0]['diagnostics'][:60]}"
    )

    system_claims = TokenClaims(
        subject="svc:analytics",
        issuer="https://idp.mercy.example",
        audience="cip-fhir",
        scopes=ScopeSet.parse("system/*.read"),
        expires_at=NOW + dt.timedelta(hours=1),
        issued_at=NOW,
        is_service_account=True,
        attributes={"service_account": "true"},
    )
    ops_context = OrganizationContext(
        principal_id="svc:analytics", organization_id=MERCY, is_named_human=False
    )
    export_request = ApiRequest(
        context=ops_context, claims=system_claims, purpose=PurposeOfUse.OPERATIONS, at=NOW
    )
    kickoff = api.kickoff_export(("Patient", "Observation"), export_request)
    print(f"  $export kickoff: {kickoff.status} -> {kickoff.headers.get('Content-Location')}")
    if not kickoff.ok:
        print(f"      refused: {kickoff.body['issue'][0]['diagnostics'][:120]}")
    job_id = kickoff.headers["Content-Location"].rsplit("/", 1)[1]
    api.run_export(job_id)
    status = api.export_status(job_id, export_request)
    print(
        f"  $export status : {status.status} files={len(status.body.get('output', []))} "
        f"expires={status.body.get('expiresAt', '')[:19]}"
    )

    treatment_export = ApiRequest(
        context=ops_context, claims=system_claims, purpose=PurposeOfUse.TREATMENT, at=NOW
    )
    print(f"  export for treatment: {api.kickoff_export(('Patient',), treatment_export).status}")

    # ---- 10. workflow ---------------------------------------------------------------------
    print(f"\n{'-' * 78}\n10. CROSS-SYSTEM WORKFLOW\n{'-' * 78}")
    orchestrator = WorkflowOrchestrator(agreements=agreements, stream=stream)
    referral = orchestrator.initiate(
        WorkflowKind.REFERRAL,
        person_id=person_id,
        context=OrganizationContext(principal_id="practitioner:jkim", organization_id=MERCY),
        performing_organization_id=ELSEWHERE,
        at=NOW,
    )
    print(f"  initiated      : {referral.task_id[:24]} state={referral.state}")
    for to_state, by, reason in (
        (TaskState.RECEIVED, "practitioner:ochoa", "received at St Elsewhere"),
        (TaskState.ACCEPTED, "practitioner:ochoa", "accepted, booking"),
        (TaskState.IN_PROGRESS, "practitioner:ochoa", "patient seen"),
        (TaskState.COMPLETED, "practitioner:ochoa", "consultation note returned"),
    ):
        referral = orchestrator.transition(referral.task_id, to_state, by=by, reason=reason)
    print(f"  closed loop    : {referral.state} after {len(referral.history)} transitions")

    try:
        orchestrator.initiate(
            WorkflowKind.IMAGING_ORDER,
            person_id=person_id,
            context=OrganizationContext(
                principal_id="practitioner:ochoa",
                organization_id=ELSEWHERE,
            ),
            performing_organization_id=IMAGING,
            at=NOW,
        )
    except Exception as exc:
        print(f"  expired agreement refuses: {str(exc)[:90]}")

    stale_orchestrator = WorkflowOrchestrator(agreements=agreements)
    old = stale_orchestrator.initiate(
        WorkflowKind.LAB_ORDER,
        person_id=person_id,
        context=OrganizationContext(principal_id="practitioner:jkim", organization_id=MERCY),
        performing_organization_id=MERCY_LAB,
        at=NOW - dt.timedelta(days=5),
    )
    print(
        f"  stale detection: {len(stale_orchestrator.stale_tasks(now=NOW))} open past threshold "
        f"({old.kind.value} after {old.age_hours(NOW):.0f}h, limit {old.kind.stale_after_hours}h)"
    )

    # ---- 11. dashboards -------------------------------------------------------------------
    print(f"\n{'-' * 78}\n11. DASHBOARDS\n{'-' * 78}")
    dashboards = DashboardRegistry()
    for partition in range(stream.partition_count):
        for record in stream.read(partition, limit=1000):
            dashboards.observe(record, at=NOW)
    dashboards.set_operational(
        ordering_violations=len(stream.ordering_violations()),
        dead_letters=dead_letters.depth(),
        break_glass=len(audit.break_glass_entries()),
        review_queue_depth=empi.review_queue_depth(),
        consumer_lag=consumer.total_lag(),
    )
    for audience in (Audience.OPERATIONAL, Audience.CLINICAL, Audience.EXECUTIVE):
        print(dashboards.snapshot(audience, now=NOW).render())

    # ---- 12. load simulation --------------------------------------------------------------
    print(f"\n{'-' * 78}\n12. LOAD SIMULATION\n{'-' * 78}")
    load_empi = EmpiIndex()
    load_repositories = RepositoryRegistry()
    load_stream = EventStream(partitions=16)
    load_engine = IntegrationEngine(
        empi=load_empi, repositories=load_repositories, stream=load_stream
    )
    load_engine.register(
        Channel(
            name="load",
            source_system="LOADGEN",
            organization_id=MERCY,
            mapping=engine_mapping,
        )
    )
    template = (
        "MSH|^~\\&|ADT|MERCY|CIP|CIPFAC|20260320080000||ADT^A01|LOAD{n:05d}|P|2.5.1\r"
        "EVN|A01|20260320080000\r"
        "PID|1||LD{n:05d}^^^MERCY^MR||{family}^{given}||19{y:02d}0{m}1{d}|{s}|||"
        "1 Main St^^Boston^MA^021{z:02d}||617555{n:04d}\r"
        + segment("PV1", f1="1", f2="I", f3="WARD", f19="LV{n:05d}")
        + "\r"
    )
    volume = 500
    started = time.perf_counter()
    for n in range(volume):
        await load_engine.ingest(
            template.format(
                n=n,
                y=50 + (n % 40),
                m=(n % 9) + 1,
                d=(n % 8) + 1,
                s="MF"[n % 2],
                z=n % 90,
                family=LOAD_SURNAMES[n % len(LOAD_SURNAMES)],
                given=LOAD_GIVENS[(n // 7) % len(LOAD_GIVENS)],
            ),
            channel_name="load",
            at=NOW,
        )
    elapsed = time.perf_counter() - started
    load_bench = Bench("load: end-to-end ingest (traced)", volume, elapsed)
    print(
        f"  {volume} messages in {elapsed:.2f}s = {volume / elapsed:,.0f} msg/s "
        "(with allocation tracing on; see the benchmark note)"
    )
    print(f"  empi          : {load_empi.statistics()}")
    print(f"  partitions    : {load_stream.partition_depths()}")
    print(
        f"  repository    : {load_repositories.for_organization(MERCY).statistics()['resources']}"
    )
    print(f"  dead letters  : {load_engine.dead_letters.depth()}")

    # The same volume with a pathological name distribution — every surname sharing a prefix,
    # which is what the first version of this generator produced. Without the bucket guard the
    # matcher degenerates to all-pairs and throughput collapses; with it, the useless key is
    # skipped and counted.
    degenerate = IntegrationEngine(
        empi=EmpiIndex(), repositories=RepositoryRegistry(), stream=EventStream(partitions=16)
    )
    degenerate.register(
        Channel(
            name="degenerate",
            source_system="LOADGEN",
            organization_id=MERCY,
            mapping=engine_mapping,
        )
    )
    started = time.perf_counter()
    for n in range(volume):
        await degenerate.ingest(
            template.format(
                n=n,
                y=50 + (n % 40),
                m=(n % 9) + 1,
                d=(n % 8) + 1,
                s="MF"[n % 2],
                z=n % 90,
                family=f"Patient{n:05d}",
                given="Test",
            ),
            channel_name="degenerate",
            at=NOW,
        )
    degenerate_elapsed = time.perf_counter() - started
    print(
        f"\n  degenerate names: {volume} messages in {degenerate_elapsed:.2f}s = "
        f"{volume / degenerate_elapsed:,.0f} msg/s"
    )
    print(f"  guard fired   : {degenerate._empi.statistics()['degenerate_blocking']}")

    # ---- 13. benchmarks -------------------------------------------------------------------
    print(f"\n{'-' * 78}\n13. BENCHMARKS\n{'-' * 78}")
    from cip_interop.hl7.parser import parse_message

    untraced = IntegrationEngine(
        empi=EmpiIndex(), repositories=RepositoryRegistry(), stream=EventStream(partitions=16)
    )
    untraced.register(
        Channel(
            name="bench",
            source_system="BENCH",
            organization_id=MERCY,
            mapping=engine_mapping,
        )
    )
    bench_volume = 400
    started = time.perf_counter()
    for n in range(bench_volume):
        await untraced.ingest(
            template.format(
                n=n + 900_000,
                y=50 + (n % 40),
                m=(n % 9) + 1,
                d=(n % 8) + 1,
                s="MF"[n % 2],
                z=n % 90,
                family=LOAD_SURNAMES[n % len(LOAD_SURNAMES)],
                given=LOAD_GIVENS[(n // 7) % len(LOAD_GIVENS)],
            ),
            channel_name="bench",
            at=NOW,
        )
    benches.append(Bench("hl7 ingest end-to-end", bench_volume, time.perf_counter() - started))

    sample_raw = hl7_messages()[1][1]
    iterations = 2000
    started = time.perf_counter()
    for _ in range(iterations):
        parse_message(sample_raw)
    benches.append(Bench("hl7 parse (7 segments)", iterations, time.perf_counter() - started))

    parsed = parse_message(sample_raw)
    started = time.perf_counter()
    for _ in range(iterations):
        engine_mapping.apply(parsed, organization_id=MERCY)
    benches.append(Bench("hl7 -> fhir mapping", iterations, time.perf_counter() - started))

    started = time.perf_counter()
    for _ in range(iterations):
        validate_resource(patient_resource)
    benches.append(Bench("fhir validation", iterations, time.perf_counter() - started))

    records = empi.records_for(person_id)
    if len(records) >= 2:
        started = time.perf_counter()
        for _ in range(iterations * 5):
            empi.model.compare(records[0], records[1])
        benches.append(Bench("empi pair comparison", iterations * 5, time.perf_counter() - started))

    started = time.perf_counter()
    for n in range(iterations * 5):
        stream.publish(
            ClinicalEventType.DECISION_GENERATED,
            partition_key=f"bench-{n % 500}",
            organization_id=MERCY,
        )
    benches.append(Bench("stream publish", iterations * 5, time.perf_counter() - started))

    started = time.perf_counter()
    for _ in range(iterations):
        api.read("Patient", patient_resource.id, request, organization_id=MERCY)
    benches.append(Bench("api read (full auth chain)", iterations, time.perf_counter() - started))

    for bench in (*benches, traced_ingest, traced_consent, load_bench):
        print(bench.render())

    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    print(f"\n  {'peak memory':<44} {peak / 1024 / 1024:>12.2f} MB")
    print(
        "\n  In-process, no network, no broker, no database. These are the real costs of the\n"
        "  parsing, mapping, matching, and authorisation work; the I/O a deployment adds is not\n"
        "  represented here and will dominate."
    )


def _seed_population(repository: Any) -> None:
    """A small synthetic population for the analytics section."""
    for index in range(24):
        patient_id = f"pop-{index:03d}"
        repository.create(
            Resource(
                "Patient",
                {
                    "resourceType": "Patient",
                    "id": patient_id,
                    "gender": "female" if index % 2 else "male",
                    "birthDate": f"19{40 + index % 50:02d}-0{index % 9 + 1}-1{index % 9}",
                    "name": [{"family": f"Sample{index:03d}", "given": ["Test"]}],
                    "address": [{"postalCode": "02134" if index % 3 else "03601", "state": "MA"}],
                },
            )
        )
        if index % 3 == 0:
            repository.create(
                Resource(
                    "Condition",
                    {
                        "resourceType": "Condition",
                        "id": f"cond-ckd-{index:03d}",
                        "clinicalStatus": {"coding": [{"code": "active"}]},
                        "code": {"coding": [{"code": "N18.3", "display": "CKD stage 3"}]},
                        "subject": {"reference": f"Patient/{patient_id}"},
                    },
                )
            )
        if index % 6 == 0:
            repository.create(
                Resource(
                    "Observation",
                    {
                        "resourceType": "Observation",
                        "id": f"obs-egfr-{index:03d}",
                        "status": "final",
                        "code": {"coding": [{"code": "33914-3", "display": "eGFR"}]},
                        "subject": {"reference": f"Patient/{patient_id}"},
                        "valueQuantity": {"value": 40 + index, "unit": "mL/min"},
                        "effectiveDateTime": "2026-03-01T09:00:00-05:00",
                    },
                )
            )
        if index % 9 == 0:
            repository.create(
                Resource(
                    "Condition",
                    {
                        "resourceType": "Condition",
                        "id": f"cond-dial-{index:03d}",
                        "clinicalStatus": {"coding": [{"code": "active"}]},
                        "code": {"coding": [{"code": "Z99.2", "display": "Dialysis dependence"}]},
                        "subject": {"reference": f"Patient/{patient_id}"},
                    },
                )
            )


def main() -> None:
    asyncio.run(run_demo())


if __name__ == "__main__":  # pragma: no cover
    main()
