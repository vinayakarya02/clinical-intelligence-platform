"""Reasoning, verification, and safety.

These three decide whether an answer ships, so the tests are mostly about what they *stop*.
Several are regressions for bugs the end-to-end run exposed and unit tests did not.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from cip_copilot.domain import Claim, ClaimSupport, Evidence, EvidenceKind
from cip_copilot.reasoning.aggregator import (
    aggregate_evidence,
    build_claims,
    corroborated_values,
    evidence_recency,
    extract_numbers,
)
from cip_copilot.safety.detectors import Severity, assess_safety, evidence_agreement
from cip_copilot.validation.verifier import (
    VerificationOutcome,
    verify_answer_text,
    verify_claims,
)

TENANT = uuid.UUID("11111111-1111-4111-8111-111111111111")
TODAY = dt.date(2026, 3, 20)


def _evidence(
    identifier: str,
    content: str,
    *,
    kind: EvidenceKind = EvidenceKind.DOCUMENT_CHUNK,
    effective: dt.date | None = None,
    confidence: float = 1.0,
    document_type: str | None = None,
) -> Evidence:
    return Evidence(
        id=identifier,
        kind=kind,
        content=content,
        tenant_id=TENANT,
        effective_date=effective,
        confidence=confidence,
        document_type=document_type,
    )


class TestEvidenceValidation:
    def test_confidence_must_be_a_probability(self) -> None:
        with pytest.raises(ValueError, match="confidence"):
            Evidence(
                id="e", kind=EvidenceKind.TOOL_RESULT, content="x", tenant_id=TENANT, confidence=1.5
            )

    def test_only_a_document_chunk_is_quotable(self) -> None:
        """A graph edge is an inference and a tool result a computation; quoting either
        would attribute it to a clinician."""
        assert EvidenceKind.DOCUMENT_CHUNK.is_quotable
        assert not EvidenceKind.GRAPH_RELATIONSHIP.is_quotable
        assert not EvidenceKind.TOOL_RESULT.is_quotable


class TestClaimConstruction:
    def test_a_claim_cannot_exist_without_evidence(self) -> None:
        """The structural guarantee behind "never produce unexplained conclusions"."""
        with pytest.raises(ValueError, match="no supporting evidence"):
            Claim(id="c1", statement="Potassium is high", evidence_ids=())

    def test_an_empty_statement_is_refused(self) -> None:
        with pytest.raises(ValueError, match="must not be empty"):
            Claim(id="c1", statement="   ", evidence_ids=("e1",))

    def test_support_weights_are_ordered(self) -> None:
        assert ClaimSupport.DIRECT.weight > ClaimSupport.DERIVED.weight
        assert ClaimSupport.DERIVED.weight > ClaimSupport.WEAK.weight


class TestAggregation:
    def test_deduplicates_by_content_not_id(self) -> None:
        """The same fact arrives as a structured record and as narrative text."""
        result = aggregate_evidence(
            [
                ("tool", (_evidence("a", "Potassium 5.4 mmol/L"),)),
                ("search", (_evidence("b", "potassium   5.4  MMOL/L"),)),
            ]
        )
        assert len(result.evidence) == 1
        assert result.dropped_duplicates == 1

    def test_document_chunks_outrank_graph_inferences(self) -> None:
        """A passage a clinician wrote is better support than an inference about it,
        however confidently the inference was extracted."""
        result = aggregate_evidence(
            [
                ("graph", (_evidence("g", "a causes b", kind=EvidenceKind.GRAPH_RELATIONSHIP),)),
                ("search", (_evidence("d", "the note says something"),)),
            ]
        )
        assert result.evidence[0].id == "d"

    def test_respects_the_limit(self) -> None:
        result = aggregate_evidence(
            [("s", tuple(_evidence(f"e{i}", f"finding number {i}") for i in range(20)))], limit=5
        )
        assert len(result.evidence) == 5
        assert result.dropped_over_budget == 15

    def test_structured_facts_do_not_need_question_overlap(self) -> None:
        """They were fetched because the plan asked for them, so they are on-topic."""
        claims = build_claims(
            "what is the situation",
            (_evidence("s", "Lisinopril 10 mg", kind=EvidenceKind.STRUCTURED_FACT),),
        )
        assert len(claims) == 1

    def test_corroboration_is_reported_not_asserted(self) -> None:
        """Regression: corroboration used to be emitted as claims whose words appear in no
        source, so the verifier rejected every one — eight per turn, generated to be
        discarded."""
        evidence = (
            _evidence("a", "Potassium 5.4", kind=EvidenceKind.STRUCTURED_FACT),
            _evidence("b", "potassium was 5.4 on admission"),
        )
        claims = build_claims("potassium", evidence)
        assert all("corroborated" not in c.statement for c in claims)

        corroborated = corroborated_values(claims, evidence)
        assert "5.4" in corroborated
        assert len(corroborated["5.4"]) == 2

    def test_recency_ignores_undated_evidence(self) -> None:
        """Much clinical reference material is legitimately undated."""
        assert evidence_recency((_evidence("a", "x"),), today=TODAY) == 0.5


class TestVerification:
    def test_a_supported_claim_passes(self) -> None:
        evidence = (_evidence("e1", "Serum potassium 5.4 mmol/L, above the reference range"),)
        claims = (
            Claim(
                id="c1",
                statement="Serum potassium 5.4 mmol/L above reference range",
                evidence_ids=("e1",),
                numeric_values=("5.4",),
            ),
        )
        report = verify_claims(claims, evidence)
        assert report.pass_rate == 1.0
        assert report.verified[0].verified is True

    def test_an_invented_number_is_rejected(self) -> None:
        """Exact match by design: 5.4 and 5.6 are similar strings and different facts."""
        evidence = (_evidence("e1", "Serum potassium 5.4 mmol/L"),)
        claims = (
            Claim(
                id="c1",
                statement="Serum potassium 5.6 mmol/L",
                evidence_ids=("e1",),
                numeric_values=("5.6",),
            ),
        )
        report = verify_claims(claims, evidence)
        assert not report.verified
        assert report.rejected[0][1] is VerificationOutcome.FABRICATED_NUMBER
        assert report.hallucination_rate == 1.0

    def test_an_unresolvable_citation_is_rejected(self) -> None:
        claims = (Claim(id="c1", statement="Something", evidence_ids=("missing",)),)
        report = verify_claims(claims, ())
        assert report.rejected[0][1] is VerificationOutcome.UNRESOLVED_CITATION

    def test_content_absent_from_the_evidence_is_rejected(self) -> None:
        evidence = (_evidence("e1", "Blood pressure 120 over 80"),)
        claims = (
            Claim(
                id="c1",
                statement="The renal function has deteriorated markedly since discharge",
                evidence_ids=("e1",),
            ),
        )
        report = verify_claims(claims, evidence)
        assert report.rejected[0][1] is VerificationOutcome.UNSUPPORTED_CONTENT

    def test_a_failed_claim_is_dropped_not_rewritten(self) -> None:
        """Rewriting turns reflection into a generator of unverified corrections."""
        evidence = (_evidence("e1", "Blood pressure 120 over 80"),)
        claims = (
            Claim(id="c1", statement="Creatinine has doubled", evidence_ids=("e1",)),
            Claim(id="c2", statement="Blood pressure 120 over 80", evidence_ids=("e1",)),
        )
        report = verify_claims(claims, evidence)
        assert [c.id for c in report.verified] == ["c2"]

    def test_citation_markers_are_not_read_as_clinical_values(self) -> None:
        """Regression (blocker): "[1]" was parsed as the number 1, so every answer that
        cited its sources failed numeric fidelity and no cited answer could ever ship."""
        evidence = (_evidence("e1", "Potassium 5.4 mmol/L"),)
        claims = (
            Claim(
                id="c1",
                statement="Potassium 5.4 mmol/L",
                evidence_ids=("e1",),
                numeric_values=("5.4",),
            ),
        )
        ok, problems = verify_answer_text("Potassium 5.4 mmol/L [1]", claims, evidence)
        assert ok, problems

    def test_a_number_from_nowhere_is_caught_in_the_prose(self) -> None:
        evidence = (_evidence("e1", "Potassium 5.4 mmol/L"),)
        claims = (
            Claim(
                id="c1",
                statement="Potassium 5.4 mmol/L",
                evidence_ids=("e1",),
                numeric_values=("5.4",),
            ),
        )
        ok, problems = verify_answer_text("Potassium 5.4, up from 3.2 [1]", claims, evidence)
        assert not ok
        assert "3.2" in problems[0]

    def test_a_series_inside_one_item_is_not_a_contradiction(self) -> None:
        """Regression: a lab-trend summary carries both endpoints of a trend, and comparing
        it against the observations it was computed from flagged every trend question."""
        evidence = (
            _evidence(
                "trend",
                "Potassium rising: 4.1 mmol/L on 2025-09-02 to 5.4 mmol/L on 2026-03-14",
                kind=EvidenceKind.TOOL_RESULT,
                effective=dt.date(2026, 3, 14),
            ),
            _evidence(
                "obs",
                "Potassium 5.4 mmol/L on 2026-03-14",
                kind=EvidenceKind.STRUCTURED_FACT,
                effective=dt.date(2026, 3, 14),
            ),
        )
        claims = (
            Claim(
                id="c1",
                statement="Potassium 5.4 mmol/L on 2026-03-14",
                evidence_ids=("trend", "obs"),
                numeric_values=("5.4", "2026", "03", "14"),
            ),
        )
        report = verify_claims(claims, evidence)
        assert report.pass_rate == 1.0, report.rejection_notes()

    def test_two_independent_sources_disagreeing_is_a_contradiction(self) -> None:
        evidence = (
            _evidence("a", "Potassium 5.4 mmol/L", effective=dt.date(2026, 3, 14)),
            _evidence("b", "Potassium 4.1 mmol/L", effective=dt.date(2026, 3, 14)),
        )
        claims = (
            Claim(
                id="c1",
                statement="Potassium 5.4 mmol/L",
                evidence_ids=("a", "b"),
                numeric_values=("5.4",),
            ),
        )
        report = verify_claims(claims, evidence)
        assert report.rejected[0][1] is VerificationOutcome.CONTRADICTED


class TestSafety:
    def test_no_evidence_blocks(self) -> None:
        report = assess_safety(question="anything", evidence=(), claims=(), today=TODAY)
        assert report.blocks
        assert report.blocking()[0].code == "no_evidence"

    def test_evidence_without_supported_claims_blocks(self) -> None:
        report = assess_safety(
            question="anything", evidence=(_evidence("e", "unrelated"),), claims=(), today=TODAY
        )
        assert report.blocks
        assert report.blocking()[0].code == "no_supported_claims"

    def test_an_unsurfaced_interaction_blocks(self) -> None:
        """An answer about something else, produced while an interaction sits in the data,
        must not go out silently."""
        report = assess_safety(
            question="what is the potassium",
            evidence=(_evidence("e", "Potassium 5.4"),),
            claims=(Claim(id="c", statement="Potassium 5.4", evidence_ids=("e",)),),
            answer_text="Potassium is 5.4 mmol/L.",
            tool_data={
                "interaction_pairs": [{"left": "rx:lisinopril", "right": "rx:spironolactone"}]
            },
            today=TODAY,
        )
        assert report.blocks
        assert report.blocking()[0].code == "dangerous_combination"

    def test_an_interaction_the_answer_states_does_not_block(self) -> None:
        """The interaction warning must not be suppressed by the interaction check."""
        report = assess_safety(
            question="do they interact",
            evidence=(_evidence("e", "interaction"),),
            claims=(Claim(id="c", statement="interaction", evidence_ids=("e",)),),
            answer_text="Lisinopril and spironolactone interact and raise potassium.",
            tool_data={
                "interaction_pairs": [{"left": "rx:lisinopril", "right": "rx:spironolactone"}]
            },
            today=TODAY,
        )
        assert not report.blocks
        assert any(f.code == "interaction_reported" for f in report.findings)

    def test_stale_evidence_warns_on_a_present_tense_question(self) -> None:
        report = assess_safety(
            question="what is the patient currently taking",
            evidence=(_evidence("e", "Lisinopril", effective=dt.date(2020, 1, 1)),),
            claims=(Claim(id="c", statement="Lisinopril", evidence_ids=("e",)),),
            today=TODAY,
        )
        assert any(f.code == "stale_evidence" for f in report.findings)

    def test_no_staleness_warning_when_the_question_is_not_time_sensitive(self) -> None:
        report = assess_safety(
            question="what was the admission diagnosis in 2020",
            evidence=(_evidence("e", "Pneumonia", effective=dt.date(2020, 1, 1)),),
            claims=(Claim(id="c", statement="Pneumonia", evidence_ids=("e",)),),
            today=TODAY,
        )
        assert not any(f.code == "stale_evidence" for f in report.findings)

    def test_an_ambiguous_abbreviation_is_flagged_with_its_meanings(self) -> None:
        report = assess_safety(
            question="does the patient have MS",
            evidence=(_evidence("e", "note"),),
            claims=(Claim(id="c", statement="note", evidence_ids=("e",)),),
            today=TODAY,
        )
        finding = next(f for f in report.findings if f.code == "ambiguous_term")
        assert "multiple sclerosis" in finding.message
        assert finding.severity is Severity.CAUTION

    def test_agreement_counts_kinds_not_items(self) -> None:
        """Three passages from one document are not corroboration."""
        same_kind = tuple(_evidence(f"e{i}", f"passage {i}") for i in range(3))
        mixed = (
            _evidence("a", "x", kind=EvidenceKind.DOCUMENT_CHUNK),
            _evidence("b", "y", kind=EvidenceKind.GRAPH_RELATIONSHIP),
        )
        assert evidence_agreement(mixed) > evidence_agreement(same_kind)


class TestNumberExtraction:
    def test_keeps_numbers_as_written(self) -> None:
        """ "5.40" and "5.4" are the same number and different transcriptions."""
        assert extract_numbers("Potassium 5.40 and sodium 141") == ("5.40", "141")
