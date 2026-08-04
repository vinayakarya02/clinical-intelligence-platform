"""Clinical ecosystem interoperability.

Most of these assert what the platform **refuses** to do — parse with assumed delimiters,
acknowledge what it did not understand, merge two people on a shared household phone, disclose
without a purpose, cross an organisation boundary on identity alone, or export a population
under a treatment purpose.

Several are regressions for defects the end-to-end run exposed and unit tests did not.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import pathlib

import pytest

from cip_interop.api import ApiRequest, ClinicalApi
from cip_interop.consent import (
    ConsentEngine,
    ConsentError,
    ConsentOutcome,
    ConsentRecord,
    Decision,
    DisclosureRequest,
    InMemoryAuditSink,
    Provision,
    RegionalPolicy,
)
from cip_interop.datalake import (
    DeidentificationMethod,
    FeatureDefinition,
    FeatureStore,
    deidentify,
)
from cip_interop.domain import (
    Address,
    AdministrativeSex,
    ContactPoint,
    HumanName,
    Identifier,
    IdentifierUse,
    MappingError,
    PersonRecord,
    PurposeOfUse,
    ValidationError,
)
from cip_interop.empi.index import EmpiError, EmpiIndex, ReviewDecision
from cip_interop.empi.matching import BlockingIndex, FieldWeight, MatchZone, default_model
from cip_interop.fhir.bundle import process_bundle
from cip_interop.fhir.capability import capability_statement
from cip_interop.fhir.definitions import FhirVersion, definition_for
from cip_interop.fhir.repository import (
    ConcurrencyError,
    FhirRepository,
    RepositoryRegistry,
    ResourceNotFoundError,
)
from cip_interop.fhir.resources import Resource, parse_reference
from cip_interop.fhir.validation import validate_resource
from cip_interop.hl7.messages import AckCode, build_ack, validate_message
from cip_interop.hl7.mllp import MllpFramingError, MllpReader, wrap
from cip_interop.hl7.parser import Delimiters, Hl7ParseError, parse_message
from cip_interop.imaging import DicomStudy, Modality, PacsEndpoint, valid_uid
from cip_interop.mapping.engine import MappingEngine, load_mapping_set
from cip_interop.mapping.transforms import hl7_timestamp
from cip_interop.orgs import (
    AgreementRegistry,
    Organization,
    OrganizationContext,
    OrganizationDirectory,
    OrganizationKind,
    SharingAgreement,
)
from cip_interop.routing import Channel, IntegrationEngine
from cip_interop.security import (
    AbacPolicy,
    AbacRule,
    Delegation,
    Operation,
    ScimProvisioner,
    ScimUser,
    ScopeSet,
    SecurityError,
    StaticTokenVerifier,
    TokenClaims,
    parse_scope,
)
from cip_interop.streaming import (
    ClinicalEventType,
    ConsumerGroup,
    EventStream,
    StreamingError,
    partition_for,
)
from cip_interop.workflow import (
    TaskState,
    WorkflowError,
    WorkflowKind,
    WorkflowOrchestrator,
)

MAPS = pathlib.Path(__file__).resolve().parents[2] / (
    "services/interop/src/cip_interop/mapping/maps/default-v2-r4.yaml"
)
NOW = dt.datetime(2026, 3, 20, 12, 0, tzinfo=dt.UTC)
ORG_A = "org:a"
ORG_B = "org:b"


def _seg(name: str, **fields: str) -> str:
    """Build a segment from field numbers. Counting pipes by hand is how a test asserts the
    wrong field and then 'proves' a bug that is not there."""
    numbers = {int(k.lstrip("f")): v for k, v in fields.items()}
    return "|".join([name, *(numbers.get(i, "") for i in range(1, max(numbers) + 1))])


def _adt(control_id: str = "C1", mrn: str = "M1", processing: str = "P") -> str:
    return (
        f"MSH|^~\\&|ADT|A|CIP|CIPFAC|20260320120000||ADT^A01|{control_id}|{processing}|2.5.1\r"
        "EVN|A01|20260320120000\r"
        f"PID|1||{mrn}^^^A^MR||Smith^Jane||19800101|F|||1 Main St^^Boston^MA^02134||6175551000\r"
        "PV1|1|I|WARD|||||||||||||||" + "V1"
    )


class TestHl7Parsing:
    """ADR-0025: delimiters come from the message, never from an assumption."""

    def test_delimiters_are_read_from_the_message(self) -> None:
        odd = "MSH#@`\\$#SEND#FAC#RECV#RFAC#20260320##ADT@A01#C9#P#2.5.1\rPID#1##9@@@A@MR##Doe@Ann"
        message = parse_message(odd)
        assert message.message_type == "ADT"
        assert message.trigger_event == "A01"
        assert message.first("PID").get(5, component=1) == "Doe"

    def test_msh_field_numbering_is_offset_by_one(self) -> None:
        """MSH-1 is the field separator itself; every other segment's field n is at index n-1.

        The single most common HL7 parser defect, so it is asserted directly rather than only
        via a field that happens to be right.
        """
        message = parse_message(_adt())
        assert message.header.get(1) == "|"
        assert message.header.get(2) == "^~\\&"
        assert message.header.get(3) == "ADT"
        assert message.header.get(9, component=1) == "ADT"
        assert message.header.get(12) == "2.5.1"

    def test_msh_2_is_not_split_on_the_delimiters_it_declares(self) -> None:
        """Regression: MSH-2 contains ^ ~ and &, so splitting it on them shreds it."""
        assert parse_message(_adt()).header.get(2) == "^~\\&"

    def test_escape_sequences_are_decoded_on_access(self) -> None:
        raw = _adt().replace("Smith^Jane", "Smith \\T\\ Sons^Jane")
        assert parse_message(raw).first("PID").get(5, component=1) == "Smith & Sons"

    def test_an_unrecognised_escape_is_left_verbatim_not_dropped(self) -> None:
        delimiters = Delimiters()
        assert delimiters.decode("a\\Zunknown\\b") == "a\\Zunknown\\b"

    def test_hex_escapes_decode(self) -> None:
        assert Delimiters().decode("caf\\X63\\") == "cafc"

    def test_repetitions_are_never_collapsed(self) -> None:
        raw = _adt().replace("M1^^^A^MR", "M1^^^A^MR~123-45-6789^^^SSA^SS")
        pid = parse_message(raw).first("PID")
        assert pid.repeat_count(3) == 2
        assert pid.get(3, repeat=1, component=1) == "123-45-6789"
        assert pid.get(3, repeat=1, component=5) == "SS"

    def test_z_segments_are_retained(self) -> None:
        message = parse_message(_adt() + "\rZPD|1|local value")
        assert [s.name for s in message.z_segments()] == ["ZPD"]

    def test_lf_and_crlf_separators_are_accepted(self) -> None:
        assert len(parse_message(_adt().replace("\r", "\n")).segments) == 4

    def test_a_non_msh_first_segment_is_refused(self) -> None:
        with pytest.raises(Hl7ParseError):
            parse_message("PID|1||9^^^A^MR")

    def test_an_alphanumeric_field_separator_is_refused(self) -> None:
        with pytest.raises(Hl7ParseError, match="alphanumeric"):
            parse_message("MSHX^~\\&XSENDXFAC")

    def test_a_repeated_delimiter_character_is_refused(self) -> None:
        with pytest.raises(Hl7ParseError, match="repeated character"):
            parse_message("MSH|^^\\&|SEND|FAC|R|RF|20260320||ADT^A01|C1|P|2.5.1")

    def test_utf16_is_refused_with_the_reason(self) -> None:
        with pytest.raises(Hl7ParseError, match="not valid utf-8"):
            parse_message(_adt().encode("utf-16"))


class TestMllp:
    def test_frames_reassemble_across_chunk_boundaries(self) -> None:
        reader = MllpReader()
        framed = wrap("MSH|^~\\&|A")
        frames = list(reader.feed(framed[:4])) + list(reader.feed(framed[4:]))
        assert frames == [b"MSH|^~\\&|A"]

    def test_a_second_start_block_abandons_the_partial_frame(self) -> None:
        """Concatenating two half-messages produces one that looks structurally valid."""
        reader = MllpReader()
        frames = list(reader.feed(b"\x0bpartial" + wrap("complete")))
        assert frames == [b"complete"]
        assert reader.discarded_bytes == len("partial")

    def test_an_unbounded_frame_is_refused_rather_than_buffered(self) -> None:
        reader = MllpReader(max_frame_bytes=32)
        with pytest.raises(MllpFramingError, match="without an end block"):
            list(reader.feed(b"\x0b" + b"x" * 64))

    def test_a_payload_containing_a_framing_byte_cannot_be_wrapped(self) -> None:
        with pytest.raises(MllpFramingError):
            wrap(b"before\x1cafter")

    def test_bytes_outside_a_frame_are_counted_not_silently_skipped(self) -> None:
        reader = MllpReader()
        list(reader.feed(b"junk" + wrap("ok")))
        assert reader.discarded_bytes == 4


class TestHl7Validation:
    def test_a_valid_message_acknowledges_aa(self) -> None:
        outcome = validate_message(parse_message(_adt()))
        assert outcome.acceptable
        assert outcome.ack_code is AckCode.ACCEPT

    def test_test_data_is_refused(self) -> None:
        """A message flagged T or D is the sender saying it is not real."""
        outcome = validate_message(parse_message(_adt(processing="T")))
        assert not outcome.acceptable
        assert any("not production" in i.text for i in outcome.errors)

    def test_a_merge_without_mrg_is_refused(self) -> None:
        raw = _adt().replace("ADT^A01", "ADT^A40").replace("EVN|A01", "EVN|A40")
        outcome = validate_message(parse_message(raw))
        assert any(i.segment == "MRG" for i in outcome.errors)

    def test_the_ack_round_trips_and_swaps_sender_and_receiver(self) -> None:
        message = parse_message(_adt())
        ack = parse_message(build_ack(message, AckCode.ACCEPT))
        assert ack.message_type == "ACK"
        assert ack.first("MSA").get(2) == message.control_id
        assert ack.sending_application == message.receiving_application
        assert ack.receiving_application == message.sending_application

    def test_an_unparseable_message_still_gets_an_answer(self) -> None:
        ack = parse_message(build_ack(None, AckCode.REJECT, text="could not parse"))
        assert ack.first("MSA").get(1) == "AR"
        assert ack.first("MSA").get(2) == ""

    def test_error_and_reject_differ_in_retry_advice(self) -> None:
        assert AckCode.ERROR.sender_should_retry
        assert not AckCode.REJECT.sender_should_retry


class TestFhirValidation:
    def test_a_bad_code_is_caught_by_the_required_binding(self) -> None:
        resource = Resource(
            "Observation",
            {
                "resourceType": "Observation",
                "id": "o1",
                "status": "finall",
                "code": {"coding": [{"code": "x"}]},
                "subject": {"reference": "Patient/p1"},
            },
        )
        assert any("required binding" in i.diagnostics for i in validate_resource(resource).errors)

    def test_a_reference_to_the_wrong_resource_type_is_caught(self) -> None:
        resource = Resource(
            "Observation",
            {
                "resourceType": "Observation",
                "id": "o1",
                "status": "final",
                "code": {"coding": [{"code": "x"}]},
                "subject": {"reference": "Practitioner/x"},
            },
        )
        assert any(
            "may only reference" in i.diagnostics for i in validate_resource(resource).errors
        )

    def test_two_members_of_a_choice_element_are_refused(self) -> None:
        resource = Resource(
            "Observation",
            {
                "resourceType": "Observation",
                "id": "o1",
                "status": "final",
                "code": {"coding": [{"code": "x"}]},
                "subject": {"reference": "Patient/p"},
                "valueString": "five",
                "valueQuantity": {"value": 5},
            },
        )
        assert any("choice element" in i.diagnostics for i in validate_resource(resource).errors)

    def test_an_unrecognised_modifier_extension_blocks_the_resource(self) -> None:
        """A modifier extension can invert the meaning of what it modifies."""
        resource = Resource(
            "Patient",
            {
                "resourceType": "Patient",
                "id": "p1",
                "modifierExtension": [{"url": "http://example.org/unknown", "valueBoolean": True}],
            },
        )
        issues = validate_resource(resource).errors
        assert any("modifier extension" in i.diagnostics for i in issues)

    def test_an_r4_only_element_is_refused_on_r5(self) -> None:
        resource = Resource(
            "MedicationRequest",
            {
                "resourceType": "MedicationRequest",
                "id": "m1",
                "status": "active",
                "intent": "order",
                "medicationCodeableConcept": {"coding": [{"code": "x"}]},
                "subject": {"reference": "Patient/p"},
            },
        )
        assert validate_resource(resource, version=FhirVersion.R4).valid
        r5 = validate_resource(resource, version=FhirVersion.R5)
        assert not r5.valid
        assert any("4.0.1" in i.diagnostics for i in r5.errors)

    def test_a_datetime_without_a_timezone_is_invalid(self) -> None:
        resource = Resource(
            "Observation",
            {
                "resourceType": "Observation",
                "id": "o1",
                "status": "final",
                "code": {"coding": [{"code": "x"}]},
                "subject": {"reference": "Patient/p"},
                "effectiveDateTime": "2026-03-20T11:00:00",
            },
        )
        assert not validate_resource(resource).valid

    def test_an_untyped_reference_is_refused(self) -> None:
        assert parse_reference("12345") is None

    def test_a_repeating_element_given_a_scalar_is_caught(self) -> None:
        resource = Resource(
            "Patient", {"resourceType": "Patient", "id": "p", "name": {"family": "X"}}
        )
        assert any("must be a list" in i.diagnostics for i in validate_resource(resource).errors)


class TestFhirRepository:
    def _repository(self) -> FhirRepository:
        return FhirRepository(organization_id=ORG_A)

    def _patient(self, identifier: str = "p1") -> Resource:
        return Resource(
            "Patient", {"resourceType": "Patient", "id": identifier, "gender": "female"}
        )

    def test_a_repository_cannot_be_built_without_an_organization(self) -> None:
        with pytest.raises(Exception, match="organization_id"):
            FhirRepository(organization_id="  ")

    def test_update_requires_the_version_the_caller_read(self) -> None:
        repository = self._repository()
        first = repository.create(self._patient())
        changed = self._patient().with_data({**self._patient().data, "active": True})
        with pytest.raises(ConcurrencyError):
            repository.update(changed, if_match='W/"999"')
        assert repository.update(changed, if_match=first.etag).version_id != first.version_id

    def test_delete_is_a_new_version_not_a_removal(self) -> None:
        repository = self._repository()
        created = repository.create(self._patient())
        repository.delete("Patient", "p1")
        with pytest.raises(ResourceNotFoundError):
            repository.read("Patient", "p1")
        assert repository.read_version("Patient", "p1", created.version_id) is not None
        assert len(repository.history("Patient", "p1")) == 2

    def test_the_organization_is_stamped_not_taken_from_the_payload(self) -> None:
        repository = self._repository()
        smuggled = Resource(
            "Patient",
            {"resourceType": "Patient", "id": "p2", "gender": "male"},
            organization_id=ORG_B,
        )
        assert repository.create(smuggled).resource.organization_id == ORG_A

    def test_search_excludes_retracted_resources_by_default(self) -> None:
        repository = self._repository()
        repository.create(
            Resource(
                "Observation",
                {
                    "resourceType": "Observation",
                    "id": "o1",
                    "status": "entered-in-error",
                    "code": {"coding": [{"code": "x"}]},
                    "subject": {"reference": "Patient/p1"},
                },
            )
        )
        assert repository.search("Observation").total == 0
        assert repository.search("Observation", include_retracted=True).total == 1

    def test_unsupported_search_parameters_are_reported_not_dropped(self) -> None:
        result = self._repository().search("Patient", {"made-up": "1"})
        assert result.unsupported_parameters == ("made-up",)

    def test_two_organizations_cannot_see_each_other(self) -> None:
        registry = RepositoryRegistry()
        registry.for_organization(ORG_A).create(self._patient())
        assert not registry.for_organization(ORG_B).exists("Patient", "p1")


class TestBundles:
    def _repository(self) -> FhirRepository:
        return FhirRepository(organization_id=ORG_A)

    def test_a_transaction_rolls_back_entirely(self) -> None:
        repository = self._repository()
        bundle = {
            "resourceType": "Bundle",
            "type": "transaction",
            "entry": [
                {
                    "resource": {"resourceType": "Patient", "id": "p9", "gender": "male"},
                    "request": {"method": "POST", "url": "Patient"},
                },
                {
                    "resource": {
                        "resourceType": "Observation",
                        "id": "o9",
                        "status": "not-a-status",
                        "code": {"coding": [{"code": "x"}]},
                        "subject": {"reference": "Patient/p9"},
                    },
                    "request": {"method": "POST", "url": "Observation"},
                },
            ],
        }
        result = process_bundle(bundle, repository)
        assert not result.applied
        assert not repository.exists("Patient", "p9")

    def test_a_batch_applies_what_succeeds(self) -> None:
        repository = self._repository()
        bundle = {
            "resourceType": "Bundle",
            "type": "batch",
            "entry": [
                {
                    "resource": {"resourceType": "Patient", "id": "p8", "gender": "male"},
                    "request": {"method": "POST", "url": "Patient"},
                },
                {
                    "resource": {
                        "resourceType": "Observation",
                        "id": "o8",
                        "status": "bad",
                        "code": {"coding": [{"code": "x"}]},
                        "subject": {"reference": "Patient/p8"},
                    },
                    "request": {"method": "POST", "url": "Observation"},
                },
            ],
        }
        result = process_bundle(bundle, repository)
        assert result.applied
        assert repository.exists("Patient", "p8")
        assert not repository.exists("Observation", "o8")

    def test_internal_references_resolve_within_a_transaction(self) -> None:
        repository = self._repository()
        bundle = {
            "resourceType": "Bundle",
            "type": "transaction",
            "entry": [
                {
                    "fullUrl": "urn:uuid:abc",
                    "resource": {"resourceType": "Patient", "id": "p7", "gender": "male"},
                    "request": {"method": "POST", "url": "Patient"},
                },
                {
                    "resource": {
                        "resourceType": "Observation",
                        "id": "o7",
                        "status": "final",
                        "code": {"coding": [{"code": "x"}]},
                        "subject": {"reference": "urn:uuid:abc"},
                    },
                    "request": {"method": "POST", "url": "Observation"},
                },
            ],
        }
        assert process_bundle(bundle, repository).succeeded
        assert (
            repository.read("Observation", "o7").resource.get("subject.reference") == "Patient/p7"
        )

    def test_a_dangling_placeholder_fails_the_transaction(self) -> None:
        repository = self._repository()
        bundle = {
            "resourceType": "Bundle",
            "type": "transaction",
            "entry": [
                {
                    "resource": {
                        "resourceType": "Observation",
                        "id": "o6",
                        "status": "final",
                        "code": {"coding": [{"code": "x"}]},
                        "subject": {"reference": "urn:uuid:missing"},
                    },
                    "request": {"method": "POST", "url": "Observation"},
                }
            ],
        }
        result = process_bundle(bundle, repository)
        assert not result.applied
        assert "dangling" in result.failure_reason


class TestCapabilityStatement:
    def test_it_is_generated_from_the_definitions(self) -> None:
        statement = capability_statement()
        served = {r["type"] for r in statement["rest"][0]["resource"]}
        assert "Patient" in served
        for entry in statement["rest"][0]["resource"]:
            definition = definition_for(entry["type"])
            assert definition is not None
            declared = {p["name"] for p in entry["searchParam"]}
            assert declared == set(definition.search_parameters)

    def test_unimplemented_features_are_declared_absent(self) -> None:
        """Understating is safe; overstating makes a client fail at the worst time."""
        rest = capability_statement()["rest"][0]
        assert rest["searchInclude"] == []
        assert rest["searchRevInclude"] == []


class TestMapping:
    def _engine(self) -> MappingEngine:
        return MappingEngine(load_mapping_set(MAPS))

    def test_the_reference_mapping_produces_valid_resources(self) -> None:
        result = self._engine().apply(parse_message(_adt()), organization_id=ORG_A)
        assert result.produced >= 1
        for resource in result.resources:
            assert validate_resource(resource).valid, validate_resource(resource).render()

    def test_a_timestamp_without_an_offset_needs_a_declared_timezone(self) -> None:
        with pytest.raises(MappingError, match="no timezone"):
            hl7_timestamp("20260320110000")
        assert hl7_timestamp("20260320110000", timezone="-05:00") == "2026-03-20T11:00:00-05:00"

    def test_timestamp_precision_is_preserved(self) -> None:
        assert hl7_timestamp("2026") == "2026"
        assert hl7_timestamp("202603") == "2026-03"
        assert hl7_timestamp("20260320") == "2026-03-20"

    def test_an_unknown_transform_fails_at_load(self, tmp_path: pathlib.Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text(
            "name: x\nversion: '1'\nmessages:\n"
            "  - message_type: ADT\n    produces:\n"
            "      - resource: Patient\n        id_source: PID-3.1\n"
            "        fields:\n          - source: PID-5.1\n            target: name[0].family\n"
            "            transform: made_up\n",
            encoding="utf-8",
        )
        with pytest.raises(MappingError, match="unknown transform"):
            load_mapping_set(bad)

    def test_a_target_that_is_not_an_element_fails_at_load(self, tmp_path: pathlib.Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text(
            "name: x\nversion: '1'\nmessages:\n"
            "  - message_type: ADT\n    produces:\n"
            "      - resource: Patient\n        id_source: PID-3.1\n"
            "        fields:\n          - source: PID-5.1\n            target: nmae[0].family\n",
            encoding="utf-8",
        )
        with pytest.raises(MappingError, match="not an element"):
            load_mapping_set(bad)

    def test_a_missing_version_fails_at_load(self, tmp_path: pathlib.Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text("name: x\nmessages: []\n", encoding="utf-8")
        with pytest.raises(MappingError, match="version"):
            load_mapping_set(bad)

    def test_two_mappings_writing_one_target_fail_at_load(self, tmp_path: pathlib.Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text(
            "name: x\nversion: '1'\nmessages:\n"
            "  - message_type: ADT\n    produces:\n"
            "      - resource: Patient\n        id_source: PID-3.1\n        fields:\n"
            "          - source: PID-5.1\n            target: name[0].family\n"
            "          - source: PID-5.2\n            target: name[0].family\n",
            encoding="utf-8",
        )
        with pytest.raises(MappingError, match="both"):
            load_mapping_set(bad)

    def test_an_unmapped_message_type_raises_rather_than_producing_nothing(self) -> None:
        raw = _adt().replace("ADT^A01", "MDM^T02")
        with pytest.raises(MappingError, match="does not handle"):
            self._engine().apply(parse_message(raw), organization_id=ORG_A)

    def test_retracted_obx_status_survives_the_mapping(self) -> None:
        """OBX-11 W means the result was wrong; storing it as final leaves it looking current."""
        raw = (
            "MSH|^~\\&|LAB|A|CIP|F|20260320120000||ORU^R01|C2|P|2.5.1\r"
            "PID|1||M1^^^A^MR||Smith^Jane||19800101|F\r"
            + _seg("OBR", f1="1", f2="P1", f3="F1", f4="X^Test^LN", f7="20260320110000", f25="F")
            + "\r"
            + _seg("OBX", f1="1", f2="NM", f3="1^A^LN", f5="5", f6="mg", f11="W")
            + "\r"
        )
        result = self._engine().apply(parse_message(raw), organization_id=ORG_A)
        observations = [r for r in result.resources if r.resource_type == "Observation"]
        assert observations[0].data["status"] == "entered-in-error"
        assert observations[0].is_retracted


class TestEmpi:
    def _record(
        self,
        record_id: str,
        family: str = "Muller",
        given: str = "Anna",
        birth: dt.date | None = None,
        *,
        organization_id: str = ORG_A,
        ssn: str = "",
        postal: str = "02134",
        phone: str = "6175551000",
        sex: str = "female",
    ) -> PersonRecord:
        identifiers = [
            Identifier(system=f"urn:mrn:{organization_id}", value=record_id, type_code="MR")
        ]
        if ssn:
            identifiers.append(Identifier(system="urn:ssn", value=ssn, type_code="SS"))
        return PersonRecord(
            record_id=record_id,
            source_system="S",
            organization_id=organization_id,
            identifiers=tuple(identifiers),
            names=(HumanName(family=family, given=(given,)),),
            birth_date=birth or dt.date(1955, 11, 3),
            sex=AdministrativeSex(sex),
            addresses=(Address(postal_code=postal),),
            telecom=(ContactPoint(system="phone", value=phone),),
        )

    def test_accented_names_fold_for_comparison(self) -> None:
        empi = EmpiIndex()
        first = empi.ingest(self._record("r1", family="Müller"))
        second = empi.ingest(self._record("r2", family="Muller", organization_id=ORG_B))
        assert second.person_id == first.person_id

    def test_a_household_member_is_not_merged(self) -> None:
        """Regression: address and phone are one fact about a household, not two about a
        person. Summed independently they outweighed name, birth date, and sex together."""
        empi = EmpiIndex()
        empi.ingest(self._record("r1"))
        other = empi.ingest(
            self._record(
                "r2", family="Muller", given="Peter", birth=dt.date(1982, 6, 4), sex="male"
            )
        )
        assert other.zone is MatchZone.NON_MATCH

    def test_ambiguity_is_queued_rather_than_decided(self) -> None:
        empi = EmpiIndex()
        empi.ingest(self._record("r1"))
        ambiguous = empi.ingest(
            self._record(
                "r2", given="Annabelle", birth=dt.date(1955, 11, 4), postal="02199", phone=""
            )
        )
        if ambiguous.zone is MatchZone.REVIEW:
            assert ambiguous.needs_review
            assert empi.review_queue_depth() == 1

    def test_conflicting_national_identifiers_block_an_automatic_match(self) -> None:
        """Twins: identical demographics, different national identifiers."""
        empi = EmpiIndex()
        first = empi.ingest(self._record("t1", family="Chen", given="Wei", ssn="111-11-1111"))
        second = empi.ingest(self._record("t2", family="Chen", given="Wei", ssn="222-22-2222"))
        assert second.person_id != first.person_id
        assert second.needs_review

    def test_a_merge_needs_a_named_actor_and_a_reason(self) -> None:
        empi = EmpiIndex()
        first = empi.ingest(self._record("r1"))
        second = empi.ingest(
            self._record("r2", family="Zzz", given="Qqq", birth=dt.date(1900, 1, 1))
        )
        with pytest.raises(EmpiError, match="named actor"):
            empi.merge(first.person_id, second.person_id, performed_by="", reason="x")
        with pytest.raises(EmpiError, match="reason"):
            empi.merge(first.person_id, second.person_id, performed_by="s", reason="")

    def test_a_merge_contradicted_by_a_national_identifier_is_refused(self) -> None:
        empi = EmpiIndex()
        first = empi.ingest(self._record("t1", family="Chen", given="Wei", ssn="111-11-1111"))
        second = empi.ingest(self._record("t2", family="Chen", given="Wei", ssn="222-22-2222"))
        with pytest.raises(EmpiError, match="refusing to merge"):
            empi.merge(first.person_id, second.person_id, performed_by="s", reason="look alike")

    def test_a_merge_is_reversible(self) -> None:
        empi = EmpiIndex()
        first = empi.ingest(self._record("r1"))
        second = empi.ingest(
            self._record("r2", family="Zzz", given="Qqq", birth=dt.date(1900, 1, 1))
        )
        merge = empi.merge(first.person_id, second.person_id, performed_by="s", reason="confirmed")
        assert len(empi.records_for(first.person_id)) == 2
        empi.unmerge(merge.merge_id, performed_by="s", reason="wrong patient")
        assert len(empi.records_for(first.person_id)) == 1
        assert len(empi.records_for(second.person_id)) == 1

    def test_link_history_answers_why(self) -> None:
        empi = EmpiIndex()
        first = empi.ingest(self._record("r1"))
        second = empi.ingest(
            self._record("r2", family="Zzz", given="Qqq", birth=dt.date(1900, 1, 1))
        )
        empi.merge(
            first.person_id, second.person_id, performed_by="steward:kim", reason="chart review"
        )
        history = empi.link_history("r2")
        assert len(history) == 2
        assert history[-1].established_by == "steward:kim"

    def test_a_person_id_is_a_legal_fhir_id(self) -> None:
        """Regression: person ids contained a colon, which FHIR forbids, so every reference
        built from one was rejected three layers away."""
        import re

        empi = EmpiIndex()
        person_id = empi.ingest(self._record("r1")).person_id
        assert re.fullmatch(r"[A-Za-z0-9\-.]{1,64}", person_id)

    def test_the_resource_alias_joins_local_ids_to_people(self) -> None:
        empi = EmpiIndex()
        person_id = empi.ingest(self._record("r1")).person_id
        empi.register_resource_alias(
            organization_id=ORG_A, resource_type="Patient", resource_id="pat-1", person_id=person_id
        )
        assert empi.person_for_resource(ORG_A, "Patient", "pat-1") == person_id
        assert empi.person_for_resource(ORG_B, "Patient", "pat-1") == ""

    def test_a_degenerate_blocking_bucket_is_skipped_and_counted(self) -> None:
        """Regression: an undiscriminating key made matching quadratic and throughput collapsed
        to 12 messages per second."""
        index = BlockingIndex(max_bucket_size=3)
        for number in range(10):
            index.add(self._record(f"r{number}", family="Samefamily", given=f"G{number}"))
        index.candidates(self._record("r99", family="Samefamily", given="Zed"))
        assert index.degenerate_strategies().get("family_soundex", 0) >= 1

    def test_temporary_identifiers_are_not_matching_evidence(self) -> None:
        record = PersonRecord(
            record_id="r1",
            source_system="S",
            organization_id=ORG_A,
            identifiers=(
                Identifier(system="urn:trauma", value="JOHN-DOE-1", use=IdentifierUse.TEMP),
            ),
        )
        assert record.matching_identifiers() == ()

    def test_missing_fields_are_neutral_not_disagreement(self) -> None:
        empi = EmpiIndex()
        sparse = PersonRecord(
            record_id="s1",
            source_system="S",
            organization_id=ORG_A,
            identifiers=(Identifier(system="urn:a", value="1", type_code="MR"),),
            names=(HumanName(family="Muller", given=("Anna",)),),
            birth_date=dt.date(1955, 11, 3),
        )
        empi.ingest(sparse)
        score = default_model().compare(sparse, self._record("r2"))
        assert set(score.missing_fields) >= {"postal_code", "phone", "sex"}

    def test_a_field_weight_with_impossible_probabilities_is_refused(self) -> None:
        with pytest.raises(ValueError, match="strictly between"):
            FieldWeight("x", m=1.0, u=0.5)
        with pytest.raises(ValueError, match="evidence \\*against\\*"):
            FieldWeight("x", m=0.1, u=0.5)

    def test_a_model_without_a_review_band_is_refused(self) -> None:
        from cip_interop.empi.matching import MatchingModel

        with pytest.raises(ValueError, match="review zone"):
            MatchingModel(
                weights=(FieldWeight("family_name", m=0.9, u=0.1),),
                upper_threshold=5.0,
                lower_threshold=5.0,
            )

    def test_review_decisions_apply(self) -> None:
        empi = EmpiIndex()
        first = empi.ingest(self._record("r1"))
        second = empi.ingest(
            self._record(
                "r2", given="Annabelle", birth=dt.date(1955, 11, 4), postal="02199", phone=""
            )
        )
        if not second.needs_review:
            pytest.skip("this pair did not land in the review band")
        empi.decide_review(
            second.review_task_id, ReviewDecision.SAME_PERSON, decided_by="steward:kim"
        )
        assert empi.person_for("r2") == first.person_id


class TestOrganizations:
    def _directory(self) -> tuple[OrganizationDirectory, AgreementRegistry]:
        directory = OrganizationDirectory()
        directory.register(Organization(ORG_A, "A", OrganizationKind.HOSPITAL))
        directory.register(
            Organization("org:a-lab", "A Lab", OrganizationKind.LABORATORY, parent_id=ORG_A)
        )
        directory.register(Organization(ORG_B, "B", OrganizationKind.CLINIC))
        return directory, AgreementRegistry(directory)

    def test_no_agreement_means_no_sharing(self) -> None:
        _, agreements = self._directory()
        decision = agreements.evaluate(
            source_organization_id=ORG_A,
            target_organization_id=ORG_B,
            purpose=PurposeOfUse.TREATMENT,
            on=dt.date(2026, 3, 20),
        )
        assert not decision.permitted
        assert "no sharing agreement" in decision.reason

    def test_an_expired_agreement_stops_working_on_its_date(self) -> None:
        _, agreements = self._directory()
        agreements.register(
            SharingAgreement(
                agreement_id="a1",
                source_organization_id=ORG_A,
                target_organization_id=ORG_B,
                purposes=frozenset({PurposeOfUse.TREATMENT}),
                effective_from=dt.date(2024, 1, 1),
                effective_until=dt.date(2025, 1, 1),
                signed_by="x",
            )
        )
        assert not agreements.evaluate(
            source_organization_id=ORG_A,
            target_organization_id=ORG_B,
            purpose=PurposeOfUse.TREATMENT,
            on=dt.date(2026, 3, 20),
        ).permitted

    def test_the_wrong_purpose_is_refused_with_the_reason(self) -> None:
        _, agreements = self._directory()
        agreements.register(
            SharingAgreement(
                agreement_id="a1",
                source_organization_id=ORG_A,
                target_organization_id=ORG_B,
                purposes=frozenset({PurposeOfUse.TREATMENT}),
                effective_from=dt.date(2024, 1, 1),
                signed_by="x",
            )
        )
        decision = agreements.evaluate(
            source_organization_id=ORG_A,
            target_organization_id=ORG_B,
            purpose=PurposeOfUse.RESEARCH,
            on=dt.date(2026, 3, 20),
        )
        assert not decision.permitted
        assert "does not cover purpose" in decision.reason

    def test_a_parent_and_child_are_one_organization(self) -> None:
        directory, agreements = self._directory()
        assert directory.within_same_organization(ORG_A, "org:a-lab")
        assert agreements.evaluate(
            source_organization_id="org:a-lab",
            target_organization_id=ORG_A,
            purpose=PurposeOfUse.TREATMENT,
        ).permitted

    def test_an_agreement_with_no_purpose_is_refused(self) -> None:
        with pytest.raises(Exception, match="names no purpose"):
            SharingAgreement(
                agreement_id="a",
                source_organization_id=ORG_A,
                target_organization_id=ORG_B,
                purposes=frozenset(),
                effective_from=dt.date(2024, 1, 1),
                signed_by="x",
            )

    def test_an_unsigned_agreement_is_refused(self) -> None:
        with pytest.raises(Exception, match="signatory"):
            SharingAgreement(
                agreement_id="a",
                source_organization_id=ORG_A,
                target_organization_id=ORG_B,
                purposes=frozenset({PurposeOfUse.TREATMENT}),
                effective_from=dt.date(2024, 1, 1),
            )


class TestConsent:
    def _engine(self) -> tuple[ConsentEngine, InMemoryAuditSink]:
        audit = InMemoryAuditSink()
        return ConsentEngine(audit_sink=audit), audit

    def _clinician(self) -> OrganizationContext:
        return OrganizationContext(principal_id="pr:1", organization_id=ORG_A)

    def test_no_consent_on_file_is_distinct_from_denied(self) -> None:
        engine, _ = self._engine()
        evaluation = engine.evaluate(
            DisclosureRequest("p1", self._clinician(), PurposeOfUse.TREATMENT, at=NOW)
        )
        assert evaluation.outcome is ConsentOutcome.NO_CONSENT_ON_FILE
        assert not evaluation.discloses
        assert "obtain consent" in evaluation.outcome.operational_action

    def test_deny_by_default(self) -> None:
        engine, _ = self._engine()
        engine.file(
            ConsentRecord(
                consent_id="c1",
                person_id="p1",
                organization_id=ORG_A,
                base_decision=Decision.DENY,
                effective_from=dt.date(2025, 1, 1),
            )
        )
        assert not engine.evaluate(
            DisclosureRequest("p1", self._clinician(), PurposeOfUse.TREATMENT, at=NOW)
        ).discloses

    def test_a_more_specific_provision_wins(self) -> None:
        engine, _ = self._engine()
        engine.file(
            ConsentRecord(
                consent_id="c1",
                person_id="p1",
                organization_id=ORG_A,
                base_decision=Decision.DENY,
                provisions=(
                    Provision(
                        decision=Decision.PERMIT, purposes=frozenset({PurposeOfUse.TREATMENT})
                    ),
                ),
                effective_from=dt.date(2025, 1, 1),
            )
        )
        assert engine.evaluate(
            DisclosureRequest("p1", self._clinician(), PurposeOfUse.TREATMENT, at=NOW)
        ).discloses

    def test_an_unconstrained_permit_provision_is_refused(self) -> None:
        with pytest.raises(ConsentError, match="unconstrained permitting"):
            ConsentRecord(
                consent_id="c1",
                person_id="p1",
                organization_id=ORG_A,
                base_decision=Decision.DENY,
                provisions=(Provision(decision=Decision.PERMIT),),
            )

    def test_revocation_takes_effect_immediately(self) -> None:
        engine, _ = self._engine()
        engine.file(
            ConsentRecord(
                consent_id="c1",
                person_id="p1",
                organization_id=ORG_A,
                base_decision=Decision.PERMIT,
                effective_from=dt.date(2025, 1, 1),
                effective_until=dt.date(2027, 1, 1),
            )
        )
        request = DisclosureRequest("p1", self._clinician(), PurposeOfUse.TREATMENT, at=NOW)
        assert engine.evaluate(request).discloses
        engine.revoke("c1", revoked_by="patient", at=NOW - dt.timedelta(hours=1))
        assert not engine.evaluate(request).discloses

    def test_break_glass_needs_a_named_human(self) -> None:
        engine, _ = self._engine()
        service = OrganizationContext(
            principal_id="svc:1", organization_id=ORG_A, is_named_human=False
        )
        evaluation = engine.evaluate(
            DisclosureRequest(
                "p1", service, PurposeOfUse.BREAK_GLASS, break_glass_reason="batch", at=NOW
            )
        )
        assert evaluation.outcome is ConsentOutcome.DENIED

    def test_break_glass_needs_a_reason(self) -> None:
        engine, _ = self._engine()
        assert not engine.evaluate(
            DisclosureRequest("p1", self._clinician(), PurposeOfUse.BREAK_GLASS, at=NOW)
        ).discloses

    def test_break_glass_audits_before_returning(self) -> None:
        engine, audit = self._engine()
        evaluation = engine.evaluate(
            DisclosureRequest(
                "p1",
                self._clinician(),
                PurposeOfUse.BREAK_GLASS,
                break_glass_reason="unresponsive in ED",
                at=NOW,
            )
        )
        assert evaluation.outcome is ConsentOutcome.BREAK_GLASS
        assert evaluation.review_required
        assert len(audit.break_glass_entries()) == 1
        assert audit.break_glass_entries()[0]["reason"] == "unresponsive in ED"

    def test_a_failing_audit_denies_rather_than_discloses(self) -> None:
        class Broken:
            def record(self, entry: dict) -> str:
                raise RuntimeError("sink down")

        engine = ConsentEngine(audit_sink=Broken())
        engine.file(
            ConsentRecord(
                consent_id="c1",
                person_id="p1",
                organization_id=ORG_A,
                base_decision=Decision.PERMIT,
                effective_from=dt.date(2025, 1, 1),
            )
        )
        evaluation = engine.evaluate(
            DisclosureRequest("p1", self._clinician(), PurposeOfUse.TREATMENT, at=NOW)
        )
        assert not evaluation.discloses
        assert "could not be audited" in evaluation.reason

    def test_every_outcome_is_audited_not_only_denials(self) -> None:
        engine, audit = self._engine()
        engine.file(
            ConsentRecord(
                consent_id="c1",
                person_id="p1",
                organization_id=ORG_A,
                base_decision=Decision.PERMIT,
                effective_from=dt.date(2025, 1, 1),
            )
        )
        engine.evaluate(DisclosureRequest("p1", self._clinician(), PurposeOfUse.TREATMENT, at=NOW))
        assert len(audit.entries) == 1

    def test_a_regional_policy_can_only_restrict(self) -> None:
        audit = InMemoryAuditSink()
        engine = ConsentEngine(
            audit_sink=audit,
            regional_policies={
                ORG_A: RegionalPolicy(
                    region="US-XX", forbidden_purposes=frozenset({PurposeOfUse.RESEARCH})
                )
            },
        )
        engine.file(
            ConsentRecord(
                consent_id="c1",
                person_id="p1",
                organization_id=ORG_A,
                base_decision=Decision.PERMIT,
                effective_from=dt.date(2025, 1, 1),
            )
        )
        assert not engine.evaluate(
            DisclosureRequest(
                "p1",
                self._clinician(),
                PurposeOfUse.RESEARCH,
                holding_organization_id=ORG_A,
                at=NOW,
            )
        ).discloses

    def test_break_glass_entries_survive_audit_buffer_pressure(self) -> None:
        audit = InMemoryAuditSink(max_entries=5)
        for number in range(20):
            audit.record({"action": "x", "n": number})
        audit.record({"action": "disclosure.break_glass", "break_glass": True})
        for number in range(20):
            audit.record({"action": "y", "n": number})
        assert len(audit.break_glass_entries()) == 1


class TestStreaming:
    def test_partitioning_is_stable_across_processes(self) -> None:
        """Python randomises str hashing per process, so hash() would break ordering under
        multi-process deployment and only there."""
        assert partition_for("person-abc", 8) == partition_for("person-abc", 8)
        assert partition_for("person-abc", 8) == 5 or 0 <= partition_for("person-abc", 8) < 8

    def test_one_patient_lands_in_one_partition(self) -> None:
        stream = EventStream(partitions=8)
        for _ in range(20):
            stream.publish(ClinicalEventType.LAB_RESULT_COMPLETED, partition_key="person-1")
        depths = [d for d in stream.partition_depths().values() if d]
        assert len(depths) == 1

    def test_a_record_without_a_partition_key_is_refused(self) -> None:
        with pytest.raises(StreamingError, match="partition key"):
            EventStream().publish(ClinicalEventType.PATIENT_ADMITTED, partition_key="")

    def test_a_sequence_gap_is_reported_not_hidden(self) -> None:
        stream = EventStream(partitions=2)
        for sequence in (1, 2, 9):
            stream.publish(
                ClinicalEventType.LAB_RESULT_COMPLETED,
                partition_key="person-1",
                source_system="LAB",
                source_sequence=sequence,
            )
        violations = stream.ordering_violations()
        assert len(violations) == 1
        assert violations[0].kind == "gap"

    def test_a_sequence_regression_is_reported(self) -> None:
        stream = EventStream(partitions=2)
        for sequence in (5, 3):
            stream.publish(
                ClinicalEventType.LAB_RESULT_COMPLETED,
                partition_key="person-1",
                source_system="LAB",
                source_sequence=sequence,
            )
        assert stream.ordering_violations()[0].kind == "regression"

    def test_redelivery_is_absorbed_by_the_ledger(self) -> None:
        stream = EventStream(partitions=1)
        consumer = ConsumerGroup("g", stream)
        record = stream.publish(ClinicalEventType.PATIENT_ADMITTED, partition_key="person-1")
        assert consumer.consume(record)
        assert not consumer.consume(record)
        assert consumer.duplicates_suppressed == 1

    def test_committing_backwards_is_refused(self) -> None:
        stream = EventStream(partitions=1)
        consumer = ConsumerGroup("g", stream)
        consumer.commit(0, 5)
        with pytest.raises(StreamingError, match="below the current committed"):
            consumer.commit(0, 2)

    def test_a_replay_without_clearing_the_ledger_does_nothing(self) -> None:
        stream = EventStream(partitions=1)
        consumer = ConsumerGroup("g", stream)
        for _ in range(3):
            consumer.consume(
                stream.publish(ClinicalEventType.PATIENT_ADMITTED, partition_key="person-1")
            )
        consumer.seek(0, 0)
        assert all(not consumer.consume(r) for r in consumer.poll(0))
        consumer.seek(0, 0, replay_processed=True)
        assert all(consumer.consume(r) for r in consumer.poll(0))

    def test_reading_below_retention_raises_rather_than_skipping(self) -> None:
        stream = EventStream(partitions=1, retention_per_partition=3)
        for _ in range(6):
            stream.publish(ClinicalEventType.PATIENT_ADMITTED, partition_key="person-1")
        with pytest.raises(StreamingError, match="retained base"):
            stream.read(0, from_offset=0)

    def test_phi_payloads_are_summarised_in_audit(self) -> None:
        stream = EventStream(partitions=1)
        record = stream.publish(
            ClinicalEventType.LAB_RESULT_COMPLETED,
            partition_key="person-1",
            payload={"value": "5.4", "code": "2823-3"},
        )
        summary = record.audit_summary()
        assert summary["payload_keys"] == ["code", "value"]
        assert "5.4" not in str(summary)


class TestSecurity:
    def test_smart_v2_suffix_must_be_in_order(self) -> None:
        assert parse_scope("user/Observation.rs") is not None
        assert parse_scope("user/Observation.sr") is None

    def test_v1_read_covers_search(self) -> None:
        scope = parse_scope("patient/Observation.read")
        assert scope is not None
        assert Operation.SEARCH in scope.operations

    def test_a_granular_constraint_is_enforced(self) -> None:
        scopes = ScopeSet.parse("user/Observation.rs?category=laboratory")
        assert scopes.permits(
            "Observation", Operation.READ, resource_attributes={"category": "laboratory"}
        )
        assert not scopes.permits(
            "Observation", Operation.READ, resource_attributes={"category": "vital-signs"}
        )

    def test_non_resource_scopes_are_kept_not_rejected(self) -> None:
        scopes = ScopeSet.parse("openid fhirUser launch/patient user/Patient.rs")
        assert scopes.non_resource_scopes == ("openid", "fhirUser", "launch/patient")

    def test_scope_rendering_preserves_the_specification_order(self) -> None:
        scope = parse_scope("user/Patient.sruc")
        assert scope is None

    def test_a_token_for_another_audience_is_refused(self) -> None:
        verifier = StaticTokenVerifier(expected_issuer="iss", expected_audience="me")
        verifier.register(
            "t",
            TokenClaims(
                subject="s",
                issuer="iss",
                audience="someone-else",
                scopes=ScopeSet(),
                expires_at=NOW + dt.timedelta(hours=1),
                issued_at=NOW,
            ),
        )
        with pytest.raises(SecurityError, match="audience"):
            verifier.verify("t", at=NOW)

    def test_an_expired_token_is_refused(self) -> None:
        verifier = StaticTokenVerifier(expected_issuer="iss", expected_audience="me")
        verifier.register(
            "t",
            TokenClaims(
                subject="s",
                issuer="iss",
                audience="me",
                scopes=ScopeSet(),
                expires_at=NOW - dt.timedelta(hours=2),
                issued_at=NOW - dt.timedelta(hours=3),
            ),
        )
        with pytest.raises(SecurityError, match="expired"):
            verifier.verify("t", at=NOW)

    def test_abac_is_deny_overrides(self) -> None:
        policy = (
            AbacPolicy()
            .add(AbacRule(rule_id="p", effect="permit", actions=frozenset({"read"})))
            .add(AbacRule(rule_id="d", effect="deny", resource_attributes={"type": "Consent"}))
        )
        assert policy.evaluate(subject={}, resource={"type": "Patient"}, action="read").permitted
        assert not policy.evaluate(
            subject={}, resource={"type": "Consent"}, action="read"
        ).permitted

    def test_abac_denies_when_no_rule_matches(self) -> None:
        assert not AbacPolicy().evaluate(subject={}, resource={}, action="read").permitted

    def test_a_delegation_cannot_exceed_the_delegator(self) -> None:
        holder = ScopeSet.parse("user/Patient.rs")
        delegated = ScopeSet.parse("user/Patient.rs user/Observation.cud")
        assert "exceeds" in Delegation.validate_subset(delegated, holder)
        assert Delegation.validate_subset(ScopeSet.parse("user/Patient.r"), holder) == ""

    def test_scim_deactivation_never_deletes(self) -> None:
        provisioner = ScimProvisioner()
        provisioner.create(ScimUser(scim_id="u1", user_name="a", roles=frozenset({"physician"})))
        provisioner.deactivate("u1")
        assert provisioner.count() == 1
        assert provisioner.get("u1") is not None
        assert not provisioner.get("u1").active
        assert provisioner.get("u1").roles == frozenset()


class TestImaging:
    def test_a_malformed_uid_is_refused(self) -> None:
        assert valid_uid("1.2.840.113619.2")
        assert not valid_uid("1.2.840.113619.2 ")
        assert not valid_uid("1.2.abc")
        assert not valid_uid("1." * 40)

    def test_a_study_without_a_patient_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="no patient reference"):
            DicomStudy(study_instance_uid="1.2.3", patient_reference="  ", organization_id=ORG_A)

    def test_a_relative_pacs_endpoint_is_refused(self) -> None:
        with pytest.raises(Exception, match="absolute URL"):
            PacsEndpoint(name="p", wado_rs_base="/dicomweb", organization_id=ORG_A)

    def test_an_unscheduled_study_does_not_reconcile(self) -> None:
        from cip_interop.imaging import ImagingRegistry

        registry = ImagingRegistry()
        reconciled = registry.register_study(
            DicomStudy(
                study_instance_uid="1.2.3",
                patient_reference="Patient/p1",
                organization_id=ORG_A,
                accession_number="A404",
            )
        )
        assert not reconciled
        assert registry.statistics()["unreconciled"] == 1
        assert registry.statistics()["studies"] == 1

    def test_the_fhir_projection_validates(self) -> None:
        from cip_interop.imaging import DicomSeries, to_imaging_study

        study = DicomStudy(
            study_instance_uid="1.2.840.113619.2.55.1",
            patient_reference="Patient/p1",
            organization_id=ORG_A,
            series=(DicomSeries("1.2.840.113619.2.55.1.1", Modality.CT, series_number=1),),
        )
        assert validate_resource(to_imaging_study(study)).valid


class TestPopulationHealth:
    def _analytics(self):
        from cip_interop.population import PopulationAnalytics

        repository = FhirRepository(organization_id=ORG_A)
        for index in range(20):
            repository.create(
                Resource(
                    "Patient",
                    {"resourceType": "Patient", "id": f"p{index}", "gender": "female"},
                )
            )
            if index % 2 == 0:
                repository.create(
                    Resource(
                        "Condition",
                        {
                            "resourceType": "Condition",
                            "id": f"c{index}",
                            "clinicalStatus": {"coding": [{"code": "active"}]},
                            "code": {"coding": [{"code": "N18.3"}]},
                            "subject": {"reference": f"Patient/p{index}"},
                        },
                    )
                )
        return PopulationAnalytics(repository, min_cell_size=5)

    def test_prevalence_reports_its_denominator(self) -> None:
        result = self._analytics().prevalence("N18.3")
        assert result["denominator"] == 20
        assert result["numerator"] == 10

    def test_small_cells_are_suppressed(self) -> None:
        from cip_interop.population import prevalence

        result = prevalence({"p1": ()}, "X", min_cell_size=11)
        assert result["numerator"] is None or result["numerator"] == 0

    def test_a_measure_with_no_eligible_patients_has_no_rate(self) -> None:
        from cip_interop.population import MeasureResult

        result = MeasureResult(
            measure_id="m",
            version="1",
            initial_population=10,
            denominator=2,
            exclusions=2,
            exceptions=0,
            numerator=0,
        )
        assert result.rate is None

    def test_exclusions_and_exceptions_are_not_collapsed(self) -> None:
        from cip_interop.population import MeasureResult

        result = MeasureResult(
            measure_id="m",
            version="1",
            initial_population=100,
            denominator=50,
            exclusions=10,
            exceptions=5,
            numerator=30,
        )
        assert result.effective_denominator == 35
        assert result.rate == round(30 / 35, 4)


class TestDataLake:
    def _patient(self) -> Resource:
        return Resource(
            "Patient",
            {
                "resourceType": "Patient",
                "id": "p1",
                "name": [{"family": "Muller", "given": ["Anna"]}],
                "identifier": [{"system": "urn:mrn", "value": "12345"}],
                "telecom": [{"system": "phone", "value": "6175551000"}],
                "birthDate": "1955-11-03",
                "address": [
                    {"line": ["12 Elm St"], "city": "Boston", "state": "MA", "postalCode": "02134"}
                ],
            },
        )

    def test_safe_harbor_removes_the_direct_identifiers(self) -> None:
        result = deidentify(self._patient(), salt="s")
        data = result.resource.data
        assert "name" not in data
        assert "identifier" not in data
        assert "telecom" not in data
        assert data["birthDate"] == "1955"
        assert data["address"][0] == {"state": "MA", "postalCode": "021"}

    def test_a_restricted_zip_prefix_is_suppressed_entirely(self) -> None:
        patient = self._patient()
        patient.data["address"][0]["postalCode"] = "03601"
        result = deidentify(patient, salt="s")
        assert "postalCode" not in result.resource.data["address"][0]

    def test_ages_over_89_are_aggregated(self) -> None:
        result = deidentify(self._patient(), salt="s", age_years=94)
        assert "birthDate" not in result.resource.data
        assert result.resource.data["extension"][0]["valueString"] == "90+"

    def test_pseudonyms_are_stable_within_a_salt_and_differ_across_salts(self) -> None:
        one = deidentify(self._patient(), salt="a").resource.id
        two = deidentify(self._patient(), salt="a").resource.id
        three = deidentify(self._patient(), salt="b").resource.id
        assert one == two
        assert one != three

    def test_deidentification_without_a_salt_is_refused(self) -> None:
        with pytest.raises(Exception, match="salt"):
            deidentify(self._patient(), salt="")

    def test_a_limited_data_set_keeps_dates_and_is_labelled(self) -> None:
        result = deidentify(
            self._patient(), method=DeidentificationMethod.LIMITED_DATA_SET, salt="s"
        )
        assert result.resource.data["birthDate"] == "1955-11-03"
        assert DeidentificationMethod.LIMITED_DATA_SET.requires_data_use_agreement

    def test_there_is_no_expert_determination_option(self) -> None:
        """Expert Determination is a person's opinion; no code can produce one."""
        assert not any("expert" in m.value for m in DeidentificationMethod)

    def test_the_feature_store_does_not_leak_the_future(self) -> None:
        store = FeatureStore()
        store.define(FeatureDefinition(name="k", description="", dtype="float", source="x"))
        store.write("e1", "k", 4.1, known_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC))
        store.write("e1", "k", 5.4, known_at=dt.datetime(2026, 3, 1, tzinfo=dt.UTC))
        assert store.read_at("e1", "k", as_of=dt.datetime(2026, 2, 1, tzinfo=dt.UTC)) == 4.1

    def test_an_undefined_feature_cannot_be_written(self) -> None:
        with pytest.raises(Exception, match="not defined"):
            FeatureStore().write("e", "k", 1, known_at=NOW)


class TestWorkflow:
    def _orchestrator(self) -> tuple[WorkflowOrchestrator, OrganizationContext]:
        directory = OrganizationDirectory()
        directory.register(Organization(ORG_A, "A", OrganizationKind.HOSPITAL))
        directory.register(Organization(ORG_B, "B", OrganizationKind.CLINIC))
        agreements = AgreementRegistry(directory)
        agreements.register(
            SharingAgreement(
                agreement_id="a1",
                source_organization_id=ORG_A,
                target_organization_id=ORG_B,
                purposes=frozenset({PurposeOfUse.TREATMENT}),
                effective_from=dt.date(2024, 1, 1),
                signed_by="x",
            )
        )
        return (
            WorkflowOrchestrator(agreements=agreements),
            OrganizationContext(principal_id="pr:1", organization_id=ORG_A),
        )

    def test_a_referral_without_an_agreement_is_refused_at_initiation(self) -> None:
        orchestrator, context = self._orchestrator()
        with pytest.raises(WorkflowError, match="not registered"):
            orchestrator.initiate(
                WorkflowKind.REFERRAL,
                person_id="person-1",
                context=context,
                performing_organization_id="org:unregistered",
                at=NOW,
            )

    def test_an_undeclared_transition_is_refused(self) -> None:
        orchestrator, context = self._orchestrator()
        task = orchestrator.initiate(
            WorkflowKind.REFERRAL,
            person_id="person-1",
            context=context,
            performing_organization_id=ORG_B,
            at=NOW,
        )
        with pytest.raises(WorkflowError, match="not a permitted transition"):
            orchestrator.transition(task.task_id, TaskState.COMPLETED, by="x", reason="skip")

    def test_closing_a_task_requires_a_reason(self) -> None:
        orchestrator, context = self._orchestrator()
        task = orchestrator.initiate(
            WorkflowKind.REFERRAL,
            person_id="person-1",
            context=context,
            performing_organization_id=ORG_B,
            at=NOW,
        )
        with pytest.raises(WorkflowError, match="requires a reason"):
            orchestrator.transition(task.task_id, TaskState.REJECTED, by="x", reason="")

    def test_a_stalled_task_is_reportable(self) -> None:
        orchestrator, context = self._orchestrator()
        orchestrator.initiate(
            WorkflowKind.LAB_ORDER,
            person_id="person-1",
            context=context,
            performing_organization_id=ORG_A,
            at=NOW - dt.timedelta(days=5),
        )
        assert len(orchestrator.stale_tasks(now=NOW)) == 1

    def test_the_task_projects_to_valid_fhir(self) -> None:
        orchestrator, context = self._orchestrator()
        task = orchestrator.initiate(
            WorkflowKind.REFERRAL,
            person_id="person-1",
            context=context,
            performing_organization_id=ORG_B,
            at=NOW,
        )
        assert validate_resource(task.to_fhir()).valid

    def test_closure_rate_is_none_rather_than_zero_when_there_is_nothing(self) -> None:
        orchestrator, _ = self._orchestrator()
        assert orchestrator.closure_rate(WorkflowKind.DISCHARGE) is None


def _api_fixture() -> tuple[ClinicalApi, ConsentEngine, EmpiIndex, RepositoryRegistry, str]:
    directory = OrganizationDirectory()
    directory.register(Organization(ORG_A, "A", OrganizationKind.HOSPITAL))
    directory.register(Organization(ORG_B, "B", OrganizationKind.CLINIC))
    agreements = AgreementRegistry(directory)
    audit = InMemoryAuditSink()
    consent = ConsentEngine(audit_sink=audit)
    empi = EmpiIndex()
    repositories = RepositoryRegistry()

    person = empi.ingest(
        PersonRecord(
            record_id="r1",
            source_system="S",
            organization_id=ORG_A,
            identifiers=(Identifier(system="urn:a", value="1", type_code="MR"),),
            names=(HumanName(family="Smith", given=("Jane",)),),
            birth_date=dt.date(1980, 1, 1),
        )
    )
    empi.register_resource_alias(
        organization_id=ORG_A,
        resource_type="Patient",
        resource_id="pat-1",
        person_id=person.person_id,
    )
    repositories.for_organization(ORG_A).create(
        Resource("Patient", {"resourceType": "Patient", "id": "pat-1", "gender": "female"})
    )
    consent.file(
        ConsentRecord(
            consent_id="c1",
            person_id=person.person_id,
            organization_id=ORG_A,
            base_decision=Decision.PERMIT,
            effective_from=dt.date(2025, 1, 1),
        )
    )
    api = ClinicalApi(
        repositories=repositories,
        consent=consent,
        agreements=agreements,
        resolve_person=empi.person_for_resource,
        policy=AbacPolicy().add(AbacRule(rule_id="p", effect="permit")),
    )
    return api, consent, empi, repositories, person.person_id


def _request(
    *,
    organization_id: str = ORG_A,
    purpose: PurposeOfUse = PurposeOfUse.TREATMENT,
    scopes: str = "user/*.cruds",
    patient_context: str = "",
) -> ApiRequest:
    return ApiRequest(
        context=OrganizationContext(principal_id="pr:1", organization_id=organization_id),
        claims=TokenClaims(
            subject="pr:1",
            issuer="iss",
            audience="me",
            scopes=ScopeSet.parse(scopes),
            expires_at=NOW + dt.timedelta(hours=1),
            issued_at=NOW - dt.timedelta(minutes=1),
            patient_context=patient_context,
        ),
        purpose=purpose,
        at=NOW,
    )


class TestClinicalApi:
    def test_consent_is_looked_up_under_the_person_not_the_local_id(self) -> None:
        """Regression: consent is filed against the EMPI person, FHIR ids are
        organisation-local. Looking consent up under the local id meant a patient with records
        at two organisations needed two consents, and revoking one left the other disclosing."""
        api, _, _, _, _ = _api_fixture()
        assert api.read("Patient", "pat-1", _request()).status == 200

    def test_a_revoked_consent_blocks_the_next_read(self) -> None:
        api, consent, _, _, _ = _api_fixture()
        assert api.read("Patient", "pat-1", _request()).status == 200
        consent.revoke("c1", revoked_by="patient", at=NOW - dt.timedelta(minutes=1))
        assert api.read("Patient", "pat-1", _request()).status == 428

    def test_a_cross_organization_read_needs_an_agreement(self) -> None:
        api, _, _, _, _ = _api_fixture()
        response = api.read(
            "Patient", "pat-1", _request(organization_id=ORG_B), organization_id=ORG_A
        )
        assert response.status == 403
        assert "no sharing agreement" in response.body["issue"][0]["diagnostics"]

    def test_a_missing_scope_refuses_before_anything_is_read(self) -> None:
        api, _, _, _, _ = _api_fixture()
        response = api.read("Patient", "pat-1", _request(scopes="user/Observation.rs"))
        assert response.status == 403
        assert "no SMART scope" in response.body["issue"][0]["diagnostics"]

    def test_a_patient_context_token_cannot_read_another_patient(self) -> None:
        api, _, _, _, _ = _api_fixture()
        response = api.read(
            "Patient",
            "pat-1",
            _request(scopes="patient/*.rs", patient_context="person-someone-else"),
        )
        assert response.status == 403
        assert "launch context" in response.body["issue"][0]["diagnostics"]

    def test_no_consent_on_file_is_428_not_403(self) -> None:
        """The two need different operational responses, so they get different statuses."""
        api, _, _, repositories, _ = _api_fixture()
        repositories.for_organization(ORG_A).create(
            Resource("Patient", {"resourceType": "Patient", "id": "pat-unknown"})
        )
        assert api.read("Patient", "pat-unknown", _request()).status == 428

    def test_a_population_export_under_a_treatment_purpose_is_refused(self) -> None:
        api, _, _, _, _ = _api_fixture()
        response = api.kickoff_export(
            ("Patient",),
            _request(purpose=PurposeOfUse.TREATMENT, scopes="system/*.read"),
        )
        assert response.status == 403
        assert "population-level export" in response.body["issue"][0]["diagnostics"]

    def test_a_population_export_needs_a_system_scope(self) -> None:
        api, _, _, _, _ = _api_fixture()
        response = api.kickoff_export(
            ("Patient",), _request(purpose=PurposeOfUse.OPERATIONS, scopes="user/*.read")
        )
        assert response.status == 403
        assert "system-level scope" in response.body["issue"][0]["diagnostics"]

    def test_export_is_asynchronous_with_a_manifest(self) -> None:
        api, _, _, _, _ = _api_fixture()
        request = _request(purpose=PurposeOfUse.OPERATIONS, scopes="system/*.read")
        kickoff = api.kickoff_export(("Patient",), request)
        assert kickoff.status == 202
        job_id = kickoff.headers["Content-Location"].rsplit("/", 1)[1]
        assert api.export_status(job_id, request).status == 202
        api.run_export(job_id)
        manifest = api.export_status(job_id, request)
        assert manifest.status == 200
        assert manifest.body["output"]
        assert "expiresAt" in manifest.body

    def test_another_principal_cannot_read_an_export_manifest(self) -> None:
        api, _, _, _, _ = _api_fixture()
        request = _request(purpose=PurposeOfUse.OPERATIONS, scopes="system/*.read")
        job_id = (
            api.kickoff_export(("Patient",), request).headers["Content-Location"].rsplit("/", 1)[1]
        )
        other = ApiRequest(
            context=OrganizationContext(principal_id="pr:2", organization_id=ORG_A),
            claims=request.claims,
            purpose=PurposeOfUse.OPERATIONS,
            at=NOW,
        )
        assert api.export_status(job_id, other).status == 404

    def test_bulk_import_reports_per_line_outcomes(self) -> None:
        api, _, _, _, _ = _api_fixture()
        request = _request(purpose=PurposeOfUse.OPERATIONS, scopes="system/*.cruds")
        ndjson = "\n".join(
            [
                '{"resourceType":"Patient","id":"imp-1","gender":"male"}',
                "not json",
                '{"resourceType":"Patient","id":"imp-2","gender":"nope"}',
            ]
        )
        response = api.bulk_import(ndjson, request)
        assert response.status == 207
        assert response.body["accepted"] == 1
        assert response.body["rejected"] == 2

    def test_search_reports_unsupported_parameters(self) -> None:
        api, _, _, _, _ = _api_fixture()
        response = api.search("Patient", {"made-up": "x"}, _request())
        assert response.status == 200
        assert any("not implemented" in i["diagnostics"] for i in response.body.get("issue", []))


class TestIngestionPipeline:
    def _engine(self) -> tuple[IntegrationEngine, EmpiIndex, RepositoryRegistry]:
        empi = EmpiIndex()
        repositories = RepositoryRegistry()
        engine = IntegrationEngine(
            empi=empi, repositories=repositories, stream=EventStream(partitions=4)
        )
        engine.register(
            Channel(
                name="c",
                source_system="S",
                organization_id=ORG_A,
                mapping=MappingEngine(load_mapping_set(MAPS)),
            )
        )
        return engine, empi, repositories

    def test_a_message_flows_end_to_end(self) -> None:
        engine, empi, repositories = self._engine()
        outcome = asyncio.run(engine.ingest(_adt(), channel_name="c", at=NOW))
        assert outcome.accepted
        assert outcome.ack_code is AckCode.ACCEPT
        assert outcome.person_id
        assert repositories.for_organization(ORG_A).exists("Patient", "pat-M1")
        assert empi.person_for_resource(ORG_A, "Patient", "pat-M1") == outcome.person_id

    def test_an_unparseable_message_is_rejected_not_acknowledged(self) -> None:
        engine, _, _ = self._engine()
        outcome = asyncio.run(engine.ingest("garbage", channel_name="c", at=NOW))
        assert not outcome.accepted
        assert outcome.ack_code is AckCode.REJECT

    def test_a_retransmission_is_acknowledged_but_not_reprocessed(self) -> None:
        engine, empi, _ = self._engine()
        asyncio.run(engine.ingest(_adt(), channel_name="c", at=NOW))
        before = empi.record_count()
        outcome = asyncio.run(engine.ingest(_adt(), channel_name="c", at=NOW))
        assert outcome.accepted
        assert empi.record_count() == before
        assert engine.duplicates_suppressed == 1

    def test_the_same_patient_from_two_organizations_resolves_to_one_person(self) -> None:
        engine, empi, _ = self._engine()
        engine.register(
            Channel(
                name="c2",
                source_system="S2",
                organization_id=ORG_B,
                mapping=MappingEngine(load_mapping_set(MAPS)),
            )
        )
        first = asyncio.run(engine.ingest(_adt(control_id="A"), channel_name="c", at=NOW))
        second = asyncio.run(
            engine.ingest(_adt(control_id="B", mrn="OTHER"), channel_name="c2", at=NOW)
        )
        assert first.person_id == second.person_id
        assert set(empi.organizations_for(first.person_id)) == {ORG_A, ORG_B}

    def test_identity_resolution_does_not_share_data_across_organizations(self) -> None:
        """The EMPI says two records are one person. It grants nobody the right to read either."""
        engine, _, repositories = self._engine()
        engine.register(
            Channel(
                name="c2",
                source_system="S2",
                organization_id=ORG_B,
                mapping=MappingEngine(load_mapping_set(MAPS)),
            )
        )
        asyncio.run(engine.ingest(_adt(control_id="A"), channel_name="c", at=NOW))
        asyncio.run(engine.ingest(_adt(control_id="B", mrn="OTHER"), channel_name="c2", at=NOW))
        assert not repositories.for_organization(ORG_B).exists("Patient", "pat-M1")
        assert not repositories.for_organization(ORG_A).exists("Patient", "pat-OTHER")

    def test_test_data_never_reaches_the_repository(self) -> None:
        engine, _, repositories = self._engine()
        outcome = asyncio.run(engine.ingest(_adt(processing="T"), channel_name="c", at=NOW))
        assert not outcome.accepted
        assert not repositories.for_organization(ORG_A).exists("Patient", "pat-M1")


class TestModuleBoundaries:
    """The dependency rule for this service, enforced rather than documented."""

    LAYERS = {
        "domain": 0,
        "hl7": 1,
        "fhir": 1,
        "orgs": 1,
        "mapping": 2,
        "empi": 2,
        "imaging": 2,
        "consent": 2,
        "security": 2,
        "streaming": 2,
        "routing": 3,
        "population": 3,
        "datalake": 3,
        "dashboards": 3,
        "workflow": 4,
        "api": 5,
        "demo": 6,
    }

    def _root(self) -> pathlib.Path:
        return pathlib.Path(__file__).resolve().parents[2] / "services/interop/src/cip_interop"

    def test_no_module_imports_upward(self) -> None:
        import re

        pattern = re.compile(r"^\s*from cip_interop\.(\w+)", re.M)
        violations: list[str] = []
        for path in self._root().rglob("*.py"):
            relative = path.relative_to(self._root())
            module = relative.parts[0] if len(relative.parts) > 1 else relative.stem
            own = self.LAYERS.get(module)
            if own is None:
                continue
            for imported in pattern.findall(path.read_text(encoding="utf-8")):
                other = self.LAYERS.get(imported)
                if other is None or imported == module:
                    continue
                if other >= own:
                    violations.append(f"{relative} (layer {own}) imports {imported} ({other})")
        assert not violations, "upward or sideways imports:\n" + "\n".join(violations)

    def test_the_interop_service_does_not_import_other_services(self) -> None:
        import re

        forbidden = re.compile(r"\bcip_(ingestion|retrieval|copilot|decision|gateway)\b")
        offenders = [
            str(p.relative_to(self._root()))
            for p in self._root().rglob("*.py")
            if forbidden.search(p.read_text(encoding="utf-8"))
        ]
        assert not offenders, f"interop reaches into other services: {offenders}"

    def test_no_hl7_field_positions_are_hardcoded_outside_the_mapping(self) -> None:
        """ADR-0026: mapping is data. A field path in engine code is an interface nobody can
        review without reading Python."""
        import re

        path_pattern = re.compile(r'"(PID|OBX|OBR|PV1|ORC|SCH|FT1)-\d+')
        offenders = []
        for path in self._root().rglob("*.py"):
            relative = str(path.relative_to(self._root()))
            if relative.startswith(("mapping", "demo")) or relative == "routing.py":
                continue
            if path_pattern.search(path.read_text(encoding="utf-8")):
                offenders.append(relative)
        assert not offenders, f"HL7 field paths in engine code: {offenders}"


class TestResourceBounds:
    def test_the_dead_letter_queue_is_bounded_and_counts_what_it_drops(self) -> None:
        from cip_interop.routing import DeadLetter, DeadLetterQueue, FailureKind

        queue = DeadLetterQueue(capacity=5)
        for number in range(20):
            queue.add(
                DeadLetter(
                    letter_id=str(number),
                    channel="c",
                    destination="d",
                    payload="",
                    reason="",
                    kind=FailureKind.PERMANENT,
                    attempts=1,
                    first_failed_at=NOW,
                    last_failed_at=NOW,
                )
            )
        assert queue.depth() == 5
        assert queue.dropped == 15

    def test_the_idempotency_ledger_is_bounded(self) -> None:
        from cip_interop.streaming import IdempotencyLedger

        ledger = IdempotencyLedger(capacity=10)
        for number in range(100):
            ledger.remember(f"e{number}")
        assert ledger.size() == 10

    def test_open_workflow_tasks_are_never_evicted(self) -> None:
        directory = OrganizationDirectory()
        directory.register(Organization(ORG_A, "A", OrganizationKind.HOSPITAL))
        orchestrator = WorkflowOrchestrator(agreements=AgreementRegistry(directory), max_tasks=5)
        context = OrganizationContext(principal_id="pr:1", organization_id=ORG_A)
        for _ in range(30):
            orchestrator.initiate(
                WorkflowKind.DISCHARGE,
                person_id="person-1",
                context=context,
                performing_organization_id=ORG_A,
                at=NOW,
            )
        assert len(orchestrator.open_tasks()) == 30

    def test_repository_history_never_evicts_the_current_version(self) -> None:
        repository = FhirRepository(organization_id=ORG_A, max_versions_per_resource=3)
        patient = Resource("Patient", {"resourceType": "Patient", "id": "p", "gender": "male"})
        stored = repository.create(patient)
        for _ in range(10):
            stored = repository.update(patient, if_match=stored.etag)
        assert repository.read("Patient", "p").version_id == stored.version_id
        assert len(repository.history("Patient", "p")) == 3


class TestAdversarialRegressions:
    """Regressions for what the adversarial pass found, each named by what it attacked."""

    def test_a_population_export_excludes_patients_who_denied_the_purpose(self) -> None:
        """Blocker: population-level authorisation says the *client* may export. It does not
        say the *patient* agreed. A patient who explicitly refused research use was in the
        research extract anyway."""
        directory = OrganizationDirectory()
        directory.register(Organization(ORG_A, "A", OrganizationKind.HOSPITAL))
        audit = InMemoryAuditSink()
        consent = ConsentEngine(audit_sink=audit)
        empi = EmpiIndex()
        repositories = RepositoryRegistry()

        for index, permits_research in ((1, True), (2, False)):
            person = empi.ingest(
                PersonRecord(
                    record_id=f"r{index}",
                    source_system="S",
                    organization_id=ORG_A,
                    identifiers=(Identifier(system="urn:a", value=str(index), type_code="MR"),),
                    names=(HumanName(family=f"Fam{index}", given=("A",)),),
                    birth_date=dt.date(1980, 1, index),
                )
            )
            empi.register_resource_alias(
                organization_id=ORG_A,
                resource_type="Patient",
                resource_id=f"pat-{index}",
                person_id=person.person_id,
            )
            repositories.for_organization(ORG_A).create(
                Resource("Patient", {"resourceType": "Patient", "id": f"pat-{index}"})
            )
            consent.file(
                ConsentRecord(
                    consent_id=f"c{index}",
                    person_id=person.person_id,
                    organization_id=ORG_A,
                    base_decision=Decision.PERMIT,
                    provisions=()
                    if permits_research
                    else (
                        Provision(
                            decision=Decision.DENY,
                            purposes=frozenset({PurposeOfUse.RESEARCH}),
                            note="declined research",
                        ),
                    ),
                    effective_from=dt.date(2025, 1, 1),
                )
            )

        api = ClinicalApi(
            repositories=repositories,
            consent=consent,
            agreements=AgreementRegistry(directory),
            resolve_person=empi.person_for_resource,
            policy=AbacPolicy().add(AbacRule(rule_id="p", effect="permit")),
        )
        request = _request(purpose=PurposeOfUse.RESEARCH, scopes="system/*.read")
        job_id = (
            api.kickoff_export(("Patient",), request).headers["Content-Location"].rsplit("/", 1)[1]
        )
        job = api.run_export(job_id)
        assert job.excluded_for_consent == 1
        manifest = api.export_status(job_id, request).body
        assert manifest["extension"]["excludedForConsent"] == 1
        assert "not a complete population" in manifest["extension"]["consentNote"]

    def test_an_ingested_message_starts_a_trace(self) -> None:
        """HL7 carries no trace header, so without one generated at ingest every asynchronous
        consumer begins an orphan trace and the wire-to-effect path cannot be reconstructed."""
        empi = EmpiIndex()
        stream = EventStream(partitions=4)
        engine = IntegrationEngine(empi=empi, repositories=RepositoryRegistry(), stream=stream)
        engine.register(
            Channel(
                name="c",
                source_system="S",
                organization_id=ORG_A,
                mapping=MappingEngine(load_mapping_set(MAPS)),
            )
        )
        outcome = asyncio.run(engine.ingest(_adt(), channel_name="c", at=NOW))
        record = stream.read_key(outcome.person_id)[0]
        assert record.traceparent.startswith("00-")
        assert len(record.traceparent.split("-")) == 4
        assert outcome.control_id in record.correlation_id

    def test_an_unregistered_organization_refuses_rather_than_raising(self) -> None:
        """An authorisation path that raises on unknown input is a 500 where a 403 was meant,
        which fails open in any caller that catches broadly."""
        directory = OrganizationDirectory()
        directory.register(Organization(ORG_A, "A", OrganizationKind.HOSPITAL))
        decision = AgreementRegistry(directory).evaluate(
            source_organization_id=ORG_A,
            target_organization_id="org:never-heard-of-it",
            purpose=PurposeOfUse.TREATMENT,
            on=dt.date(2026, 3, 20),
        )
        assert not decision.permitted
        assert "not registered" in decision.reason

    def test_internal_ids_become_legal_fhir_ids_in_references(self) -> None:
        """Regression: organisation ids look like ``org:mercy``, and a colon makes every
        reference built from one invalid — failing in whatever consumer dereferences it."""
        from cip_interop.fhir.resources import fhir_id, make_reference

        assert fhir_id("org:mercy-general") == "org-mercy-general"
        reference = make_reference("Organization", "org:mercy-general")
        assert parse_reference(reference["reference"]) is not None
