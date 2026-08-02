"""Metadata extraction, classification, and tokenisation tests."""

from __future__ import annotations

import datetime as dt
from dataclasses import replace

import pytest

from cip_core.models.enums import DocumentType
from cip_ingestion.parsers import TextParser
from cip_ingestion.processing import (
    HeuristicTokenEstimator,
    classify_document_type,
    detect_language,
    detect_phi_indicators,
    detect_sections,
    extract_effective_date,
    extract_metadata,
    normalize_document,
    split_sentences,
)
from tests.fixtures.documents import (
    DISCHARGE_SUMMARY_TEXT,
    LAB_REPORT_TEXT,
    RADIOLOGY_NOTE_TEXT,
)


def _metadata(raw: str):  # type: ignore[no-untyped-def]
    parsed = TextParser().parse(raw.encode())
    normalized = normalize_document(parsed)
    sections = detect_sections(normalized)
    normalized = replace(normalized, sections=sections)
    return extract_metadata(
        normalized,
        sections,
        parser_properties=parsed.properties,
        page_count=parsed.page_count,
    )


class TestDocumentClassification:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            (DISCHARGE_SUMMARY_TEXT, DocumentType.DISCHARGE_SUMMARY),
            (LAB_REPORT_TEXT, DocumentType.LAB_REPORT),
            (RADIOLOGY_NOTE_TEXT, DocumentType.RADIOLOGY_NOTE),
        ],
    )
    def test_classifies_clinical_document_types(self, text: str, expected: DocumentType) -> None:
        document_type, confidence = classify_document_type(text)
        assert document_type is expected
        assert confidence > 0.0

    def test_weak_evidence_yields_unknown(self) -> None:
        """A low-confidence guess is worse than admitting ignorance."""
        document_type, confidence = classify_document_type("Some generic prose about nothing.")
        assert document_type is DocumentType.UNKNOWN
        assert confidence < 1.0

    def test_empty_text_yields_unknown(self) -> None:
        assert classify_document_type("")[0] is DocumentType.UNKNOWN

    def test_section_names_reinforce_classification(self) -> None:
        _, without = classify_document_type(DISCHARGE_SUMMARY_TEXT)
        _, with_sections = classify_document_type(
            DISCHARGE_SUMMARY_TEXT, ("hospital_course", "disposition")
        )
        assert with_sections >= without


class TestLanguageDetection:
    def test_detects_english(self) -> None:
        assert detect_language(DISCHARGE_SUMMARY_TEXT) == "en"

    def test_returns_none_for_short_text(self) -> None:
        """Too little evidence must read as 'unknown', not 'English'."""
        assert detect_language("Chest pain.") is None

    def test_returns_none_for_non_english(self) -> None:
        spanish = (
            "El paciente presenta dolor toracico intenso que comenzo hace tres dias "
            "y empeoro durante la noche previa a su ingreso hospitalario urgente."
        )
        assert detect_language(spanish) is None


class TestPhiDetection:
    @pytest.mark.parametrize(
        ("text", "expected_category"),
        [
            ("MRN: 00471925", "mrn"),
            ("SSN 123-45-6789", "ssn"),
            ("Call 555-123-4567", "phone"),
            ("Contact care@example.com", "email"),
            ("DOB: 1964-02-11", "date_of_birth"),
            ("Patient Name: Jordan Rivera", "patient_name"),
        ],
    )
    def test_detects_phi_categories(self, text: str, expected_category: str) -> None:
        assert expected_category in detect_phi_indicators(text)

    def test_returns_categories_not_values(self) -> None:
        """Recording the matched value would relocate PHI into a non-PHI column."""
        indicators = detect_phi_indicators("SSN 123-45-6789 and MRN: 00471925")
        assert "123-45-6789" not in str(indicators)
        assert "00471925" not in str(indicators)

    def test_clean_text_reports_nothing(self) -> None:
        assert detect_phi_indicators("No identifiers appear in this sentence.") == ()


class TestEffectiveDate:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Date of Service: 2026-03-14", dt.date(2026, 3, 14)),
            ("Collected: 03/14/2026", dt.date(2026, 3, 14)),
            ("Study Date: March 14, 2026", dt.date(2026, 3, 14)),
        ],
    )
    def test_extracts_labelled_dates(self, text: str, expected: dt.date) -> None:
        assert extract_effective_date(text) == expected

    def test_labelled_date_outranks_an_earlier_unlabelled_one(self) -> None:
        """A printed-on timestamp or date of birth must not become the effective date."""
        text = "Printed 2020-01-01\nDOB 1964-02-11\nDate of Service: 2026-03-14"
        assert extract_effective_date(text) == dt.date(2026, 3, 14)

    def test_implausible_dates_are_rejected(self) -> None:
        assert extract_effective_date("Reference 1802-01-01 in the archive") is None

    def test_missing_date_returns_none(self) -> None:
        assert extract_effective_date("No dates appear in this text at all.") is None

    def test_invalid_calendar_dates_are_rejected(self) -> None:
        assert extract_effective_date("Date: 2026-02-30") is None


class TestMetadataExtraction:
    def test_extracts_a_full_metadata_record(self) -> None:
        metadata = _metadata(DISCHARGE_SUMMARY_TEXT)
        assert metadata.document_type is DocumentType.DISCHARGE_SUMMARY
        assert metadata.effective_date == dt.date(2026, 3, 14)
        assert metadata.language == "en"
        assert metadata.word_count > 50
        assert "chief_complaint" in metadata.section_names

    def test_title_prefers_the_opening_line_over_a_section_heading(self) -> None:
        assert _metadata(DISCHARGE_SUMMARY_TEXT).title == "DISCHARGE SUMMARY"

    def test_producer_generated_titles_are_ignored(self) -> None:
        from cip_ingestion.domain import NormalizedDocument
        from cip_ingestion.processing.metadata import extract_metadata as extract

        normalized = NormalizedDocument(text="RADIOLOGY REPORT\n\nFindings are normal.")
        metadata = extract(
            normalized, (), parser_properties={"title": "Microsoft Word - scan001.doc"}
        )
        assert metadata.title != "Microsoft Word - scan001.doc"

    def test_metadata_serialises_to_json(self) -> None:
        payload = _metadata(DISCHARGE_SUMMARY_TEXT).to_json()
        assert isinstance(payload["section_names"], list)
        assert isinstance(payload["document_type"], str)
        assert payload["effective_date"] == "2026-03-14"


class TestTokenEstimator:
    def test_empty_text_has_no_tokens(self) -> None:
        assert HeuristicTokenEstimator().count("") == 0
        assert HeuristicTokenEstimator().count("   \n ") == 0

    def test_count_grows_with_text_length(self) -> None:
        estimator = HeuristicTokenEstimator()
        assert estimator.count("word " * 100) > estimator.count("word " * 10)

    def test_punctuation_counts_as_tokens(self) -> None:
        estimator = HeuristicTokenEstimator()
        assert estimator.count("a, b, c.") > estimator.count("a b c")

    def test_estimate_is_within_a_sane_range_of_word_count(self) -> None:
        """The estimate must track real tokenisers closely enough to size chunks."""
        text = DISCHARGE_SUMMARY_TEXT
        words = len(text.split())
        estimate = HeuristicTokenEstimator().count(text)
        assert words * 0.8 <= estimate <= words * 2.5

    def test_rejects_a_non_positive_ratio(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            HeuristicTokenEstimator(chars_per_token=0)


class TestSentenceSplitting:
    def test_splits_on_sentence_boundaries(self) -> None:
        spans = split_sentences("First sentence. Second sentence. Third one.")
        assert len(spans) == 3

    def test_does_not_split_on_clinical_abbreviations(self) -> None:
        """ "Pt. was given 2.5 mg q.i.d. per Dr. Chen." is one sentence."""
        text = "Pt. was given 2.5 mg q.i.d. per Dr. Chen."
        assert len(split_sentences(text)) == 1

    def test_does_not_split_inside_decimals(self) -> None:
        assert len(split_sentences("Troponin was 0.04 ng/mL today.")) == 1

    def test_newlines_are_hard_boundaries(self) -> None:
        """Line breaks are structural in clinical documents — one medication per line."""
        spans = split_sentences("Lisinopril 10 mg\nMetformin 500 mg\nAtorvastatin 40 mg")
        assert len(spans) == 3

    def test_offsets_index_into_the_original_text(self) -> None:
        text = "First sentence. Second sentence."
        for start, end in split_sentences(text):
            assert text[start:end].strip()

    def test_empty_text_yields_no_spans(self) -> None:
        assert split_sentences("") == []
        assert split_sentences("   \n  ") == []
