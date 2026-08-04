"""Normalisation and section-detection tests."""

from __future__ import annotations

from itertools import pairwise

import pytest

from cip_core.models.enums import SectionType
from cip_ingestion.domain import BlockKind, ParsedDocument, ParsedPage, TextBlock
from cip_ingestion.parsers import TextParser
from cip_ingestion.processing import (
    NormalizationOptions,
    detect_sections,
    match_heading,
    normalize_document,
    normalize_text,
)
from tests.fixtures.documents import DISCHARGE_SUMMARY_TEXT


def _document(*pages: list[str], kinds: dict[int, BlockKind] | None = None) -> ParsedDocument:
    """Build a ParsedDocument from lists of lines, one list per page."""
    kinds = kinds or {}
    return ParsedDocument(
        parser_name="test",
        media_type="text/plain",
        pages=tuple(
            ParsedPage(
                page_number=page_number,
                blocks=tuple(
                    TextBlock(
                        text=line,
                        kind=kinds.get(page_number, BlockKind.PARAGRAPH),
                        page_number=page_number,
                        order=order,
                    )
                    for order, line in enumerate(lines)
                ),
            )
            for page_number, lines in enumerate(pages, start=1)
        ),
    )


class TestNormalizeText:
    def test_expands_ligatures(self) -> None:
        """Ligatures break exact-match retrieval, so they must not survive."""
        assert "fibrillation" in normalize_text("Atrial ﬁbrillation noted")

    def test_normalises_typographic_punctuation(self) -> None:
        result = normalize_text("“stable” – patient’s course")
        assert '"stable"' in result
        assert "patient's" in result

    def test_strips_control_characters(self) -> None:
        assert normalize_text("clean\x00text\x07here") == "cleantexthere"

    def test_collapses_runs_of_spaces(self) -> None:
        assert normalize_text("too      many spaces") == "too many spaces"

    def test_normalises_line_endings(self) -> None:
        assert "\r" not in normalize_text("line one\r\nline two\rline three")

    def test_collapses_excess_blank_lines(self) -> None:
        assert normalize_text("a\n\n\n\n\nb") == "a\n\nb"


class TestDehyphenation:
    def test_rejoins_a_word_split_across_lines(self) -> None:
        parsed = _document(["The patient has hyper-", "tension and diabetes."])
        assert "hypertension" in normalize_document(parsed).text

    def test_does_not_rejoin_before_a_capitalised_word(self) -> None:
        """ "HIV-\\nPositive" is a compound, not a wrap artefact."""
        parsed = _document(["Patient is HIV-", "Positive per chart."])
        text = normalize_document(parsed).text
        assert "HIVPositive" not in text

    def test_can_be_disabled(self) -> None:
        parsed = _document(["hyper-", "tension"])
        options = NormalizationOptions(dehyphenate=False)
        assert "hypertension" not in normalize_document(parsed, options).text


class TestFurnitureRemoval:
    def test_repeating_headers_are_stripped(self) -> None:
        parsed = _document(
            ["MERCY GENERAL HOSPITAL", "Clinical content page one."],
            ["MERCY GENERAL HOSPITAL", "Clinical content page two."],
            ["MERCY GENERAL HOSPITAL", "Clinical content page three."],
            ["MERCY GENERAL HOSPITAL", "Clinical content page four."],
        )
        text = normalize_document(parsed).text
        assert "MERCY GENERAL HOSPITAL" not in text
        assert "Clinical content page one." in text

    def test_page_numbers_are_stripped(self) -> None:
        parsed = _document(
            ["Real clinical content here.", "Page 1 of 3"],
            ["More clinical content.", "Page 2 of 3"],
            ["Final clinical content.", "Page 3 of 3"],
        )
        text = normalize_document(parsed).text
        assert "Page 1 of 3" not in text
        assert "Real clinical content here." in text

    def test_short_documents_keep_repeated_lines(self) -> None:
        """Below the page threshold, repetition is not evidence of furniture."""
        parsed = _document(["Stable", "content"], ["Stable", "other"])
        assert "Stable" in normalize_document(parsed).text

    def test_long_repeating_lines_are_kept(self) -> None:
        """Genuine repeated prose is content, not page furniture."""
        sentence = (
            "The patient was advised to continue all home medications without alteration "
            "and to return immediately if symptoms recur or worsen in any way."
        )
        parsed = _document(*[[sentence, f"Unique line {i}"] for i in range(4)])
        assert sentence in normalize_document(parsed).text

    def test_removal_is_counted(self) -> None:
        parsed = _document(*[["HEADER", f"content {i}"] for i in range(4)])
        assert normalize_document(parsed).removed_line_count >= 4


class TestPageOffsets:
    def test_offsets_resolve_back_to_pages(self) -> None:
        parsed = _document(["First page content."], ["Second page content."])
        normalized = normalize_document(parsed)

        first = normalized.text.index("First")
        second = normalized.text.index("Second")
        assert normalized.page_for_offset(first) == 1
        assert normalized.page_for_offset(second) == 2

    def test_offsets_stay_within_the_text(self) -> None:
        normalized = normalize_document(_document(["a"], ["b"], ["c"]))
        for _page, start, end in normalized.page_offsets:
            assert 0 <= start <= end <= len(normalized.text)

    def test_blank_pages_do_not_shift_later_pages(self) -> None:
        parsed = _document(["Page one."], [], ["Page three."])
        normalized = normalize_document(parsed)
        offset = normalized.text.index("Page three.")
        assert normalized.page_for_offset(offset) == 3

    def test_table_rows_keep_their_line_structure(self) -> None:
        parsed = ParsedDocument(
            parser_name="test",
            media_type="text/plain",
            pages=(
                ParsedPage(
                    page_number=1,
                    blocks=(
                        TextBlock(
                            text="Sodium | 141 | mmol/L\nPotassium | 4.2 | mmol/L",
                            kind=BlockKind.TABLE,
                        ),
                    ),
                ),
            ),
        )
        text = normalize_document(parsed).text
        assert "Sodium | 141 | mmol/L" in text
        assert text.count("\n") >= 1


class TestHeadingMatching:
    @pytest.mark.parametrize(
        ("heading", "canonical"),
        [
            ("CHIEF COMPLAINT", "chief_complaint"),
            ("Chief Complaint:", "chief_complaint"),
            ("HPI", "history_of_present_illness"),
            ("History of Present Illness", "history_of_present_illness"),
            ("ALLERGIES", "allergies"),
            ("Discharge Medications", "medications"),
            ("ASSESSMENT AND PLAN", "assessment"),
            ("Hospital Course", "hospital_course"),
            ("LABORATORY RESULTS", "laboratory_results"),
            ("Past Medical History", "past_medical_history"),
        ],
    )
    def test_recognises_clinical_headings(self, heading: str, canonical: str) -> None:
        pattern = match_heading(heading)
        assert pattern is not None
        assert pattern.canonical_name == canonical

    def test_recognises_inline_heading_with_content(self) -> None:
        pattern = match_heading("ALLERGIES: Penicillin - rash")
        assert pattern is not None
        assert pattern.canonical_name == "allergies"

    @pytest.mark.parametrize(
        "line",
        [
            "The patient reports chest pain.",
            "",
            "Assessment of the patient's overall condition showed steady improvement "
            "throughout the admission period.",
        ],
    )
    def test_rejects_non_headings(self, line: str) -> None:
        assert match_heading(line) is None

    def test_problem_list_sections_are_typed_correctly(self) -> None:
        pattern = match_heading("PAST MEDICAL HISTORY")
        assert pattern is not None
        assert pattern.section_type is SectionType.PROBLEM_LIST


class TestSectionDetection:
    def test_detects_sections_in_a_clinical_note(self) -> None:
        parsed = TextParser().parse(DISCHARGE_SUMMARY_TEXT.encode())
        sections = detect_sections(normalize_document(parsed))
        names = {section.canonical_name for section in sections}

        assert {"chief_complaint", "history_of_present_illness", "medications"} <= names
        assert "assessment" in names

    def test_sections_are_contiguous_and_ordered(self) -> None:
        parsed = TextParser().parse(DISCHARGE_SUMMARY_TEXT.encode())
        normalized = normalize_document(parsed)
        sections = detect_sections(normalized)

        for previous, current in pairwise(sections):
            assert previous.char_end == current.char_start, "sections must not have gaps"
        assert sections[-1].char_end == len(normalized.text)

    def test_preamble_before_the_first_heading_is_retained(self) -> None:
        """The opening lines carry encounter context later sections refer back to."""
        parsed = TextParser().parse(
            b"Patient: Jordan Rivera\nMRN 00471925\n\nCHIEF COMPLAINT\nChest pain."
        )
        sections = detect_sections(normalize_document(parsed))
        assert sections[0].canonical_name == "document_preamble"
        assert sections[0].char_start == 0

    def test_detects_radiology_report_sections(self) -> None:
        """Regression: the findings body used to fall through to the document preamble.

        Radiology reports are a supported document type but used a heading vocabulary the
        detector did not know, so the clinically load-bearing part of every report was
        left unsectioned — invisible to section filters and to retrieval ranking alike.
        """
        report = (
            b"RADIOLOGY REPORT\n\nINDICATION\nChest pain.\n\nTECHNIQUE\n"
            b"Non-contrast CT of the chest.\n\nCOMPARISON\nRadiograph dated 2025-11-04.\n\n"
            b"FINDINGS\nNo focal consolidation. No pleural effusion.\n\n"
            b"IMPRESSION\nNo acute cardiopulmonary process.\n"
        )
        normalized = normalize_document(TextParser().parse(report))
        sections = detect_sections(normalized)
        by_name = {section.canonical_name: section for section in sections}

        assert {"indication", "technique", "comparison", "findings"} <= set(by_name)
        findings = by_name["findings"]
        assert "consolidation" in normalized.text[findings.char_start : findings.char_end]
        # IMPRESSION is the radiology spelling of an assessment and already mapped there.
        assert "assessment" in by_name

    def test_document_without_headings_becomes_one_body_section(self) -> None:
        parsed = TextParser().parse(b"Just some free text with no recognised structure.")
        sections = detect_sections(normalize_document(parsed))
        assert len(sections) == 1
        assert sections[0].canonical_name == "document_body"

    def test_empty_document_yields_no_sections(self) -> None:
        from cip_ingestion.domain import NormalizedDocument

        assert detect_sections(NormalizedDocument(text="")) == ()

    def test_sections_carry_page_numbers(self) -> None:
        parsed = TextParser().parse(DISCHARGE_SUMMARY_TEXT.encode())
        sections = detect_sections(normalize_document(parsed))
        assert all(section.page_start is not None for section in sections)
