"""Data-quality gating and upload-validation tests."""

from __future__ import annotations

from dataclasses import replace

import pytest

from cip_core.config import IngestionSettings
from cip_core.errors import (
    PayloadTooLargeError,
    UnsupportedMediaTypeError,
    ValidationFailedError,
)
from cip_core.models.enums import DocumentType, QualityVerdict
from cip_ingestion.domain import (
    DocumentMetadata,
    NormalizedDocument,
    ParsedDocument,
    ParsedPage,
    TextBlock,
    TextChunk,
)
from cip_ingestion.parsers import TextParser
from cip_ingestion.processing import (
    ChunkingOptions,
    StructuralSemanticChunker,
    assess_quality,
    detect_sections,
    extract_metadata,
    normalize_document,
)
from cip_ingestion.processing.quality import count_garbled_characters
from cip_ingestion.validation import (
    compute_content_hash,
    sanitize_filename,
    sniff_media_type,
    validate_upload,
)
from tests.fixtures.documents import DISCHARGE_SUMMARY_TEXT, build_docx, build_pdf


def _assess(raw: bytes):  # type: ignore[no-untyped-def]
    parsed = TextParser().parse(raw)
    normalized = normalize_document(parsed)
    sections = detect_sections(normalized)
    normalized = replace(normalized, sections=sections)
    metadata = extract_metadata(normalized, sections, page_count=parsed.page_count)
    chunks = StructuralSemanticChunker(
        ChunkingOptions(target_tokens=96, min_tokens=16, max_tokens=160)
    ).chunk(normalized)
    return assess_quality(parsed=parsed, normalized=normalized, chunks=chunks, metadata=metadata)


class TestGarbledCharacterCounting:
    def test_clean_text_has_none(self) -> None:
        assert count_garbled_characters("Normal clinical text.\nWith lines.\tAnd tabs.") == 0

    def test_replacement_characters_are_counted(self) -> None:
        assert count_garbled_characters("caf" + chr(0xFFFD) + " visit") == 1

    def test_control_characters_are_counted(self) -> None:
        assert count_garbled_characters("text" + chr(0x07) + chr(0x00)) == 2

    def test_whitespace_is_never_counted(self) -> None:
        assert count_garbled_characters("\t\n\r   ") == 0


class TestQualityAssessment:
    def test_a_healthy_document_passes(self) -> None:
        report = _assess(DISCHARGE_SUMMARY_TEXT.encode())
        assert report.verdict is QualityVerdict.PASS
        assert report.score > 0.8
        assert report.failed_checks == ()

    def test_report_serialises_every_check(self) -> None:
        payload = _assess(DISCHARGE_SUMMARY_TEXT.encode()).to_json()
        assert payload["verdict"] == "pass"
        names = {check["name"] for check in payload["checks"]}
        assert {"extraction_yield", "garbled_text", "chunking"} <= names

    def test_no_chunks_fails_the_gate(self) -> None:
        parsed = TextParser().parse(b"Some text content here for the parser to accept.")
        normalized = normalize_document(parsed)
        report = assess_quality(
            parsed=parsed,
            normalized=normalized,
            chunks=(),
            metadata=DocumentMetadata(),
        )
        assert report.verdict is QualityVerdict.FAIL
        assert "chunking" in {check.name for check in report.failed_checks}

    def test_garbled_text_fails_the_gate(self) -> None:
        """The document the gate exists to catch: parsed fine, unreadable content."""
        garbled = (chr(0xFFFD) * 400).encode("utf-8")
        parsed = TextParser().parse(garbled)
        normalized = normalize_document(parsed)
        chunks = (
            TextChunk(index=0, text=normalized.text, char_start=0, char_end=10, token_count=5),
        )
        report = assess_quality(
            parsed=parsed, normalized=normalized, chunks=chunks, metadata=DocumentMetadata()
        )
        assert report.verdict is QualityVerdict.FAIL
        assert "garbled_text" in {check.name for check in report.failed_checks}

    def test_a_critical_failure_overrides_a_good_average(self) -> None:
        """Tidy metadata must not let unreadable content average its way past the gate."""
        parsed = ParsedDocument(
            parser_name="test",
            media_type="text/plain",
            pages=(ParsedPage(page_number=1, blocks=(TextBlock(text="x"),)),),
        )
        normalized = NormalizedDocument(text="x", source_char_count=1)
        report = assess_quality(
            parsed=parsed,
            normalized=normalized,
            chunks=(),
            metadata=DocumentMetadata(title="Nice title", language="en"),
        )
        assert report.verdict is QualityVerdict.FAIL

    def test_low_ocr_confidence_is_reported(self) -> None:
        parsed = ParsedDocument(
            parser_name="pdf",
            media_type="application/pdf",
            pages=(
                ParsedPage(
                    page_number=1,
                    blocks=(TextBlock(text="Recovered but uncertain text " * 20),),
                    ocr_applied=True,
                    ocr_confidence=0.20,
                ),
            ),
        )
        normalized = normalize_document(parsed)
        chunks = (
            TextChunk(
                index=0,
                text=normalized.text,
                char_start=0,
                char_end=len(normalized.text),
                token_count=40,
            ),
        )
        report = assess_quality(
            parsed=parsed, normalized=normalized, chunks=chunks, metadata=DocumentMetadata()
        )
        ocr_check = next(c for c in report.checks if c.name == "ocr_confidence")
        assert not ocr_check.passed

    def test_ocr_check_is_absent_when_no_ocr_ran(self) -> None:
        report = _assess(DISCHARGE_SUMMARY_TEXT.encode())
        assert "ocr_confidence" not in {check.name for check in report.checks}

    def test_metadata_completeness_never_fails_a_document(self) -> None:
        """A missing date must not disqualify a clinically valuable document."""
        report = _assess(b"HOSPITAL COURSE\nPatient improved steadily over the admission.\n" * 5)
        metadata_check = next(c for c in report.checks if c.name == "metadata_completeness")
        assert metadata_check.passed


class TestContentHashing:
    def test_hash_is_stable(self) -> None:
        assert compute_content_hash(b"content") == compute_content_hash(b"content")

    def test_different_content_hashes_differently(self) -> None:
        assert compute_content_hash(b"a") != compute_content_hash(b"b")

    def test_hash_is_lowercase_hex_sha256(self) -> None:
        digest = compute_content_hash(b"x")
        assert len(digest) == 64
        assert digest == digest.lower()


class TestFilenameSanitisation:
    @pytest.mark.parametrize(
        ("raw", "expected_absent"),
        [
            ("../../etc/passwd", ".."),
            ("C:\\Windows\\system32\\cmd.exe", "\\"),
            ("/absolute/path/report.pdf", "/"),
        ],
    )
    def test_paths_are_reduced_to_a_bare_name(self, raw: str, expected_absent: str) -> None:
        result = sanitize_filename(raw)
        assert result is not None
        assert expected_absent not in result

    def test_normal_filename_is_preserved(self) -> None:
        assert sanitize_filename("discharge_summary-2026.pdf") == "discharge_summary-2026.pdf"

    def test_unusable_input_yields_none(self) -> None:
        assert sanitize_filename(None) is None
        assert sanitize_filename("") is None
        assert sanitize_filename("...") is None

    def test_long_names_are_truncated(self) -> None:
        result = sanitize_filename("a" * 500 + ".pdf")
        assert result is not None
        assert len(result) <= 255


class TestMediaTypeSniffing:
    def test_detects_pdf(self) -> None:
        assert sniff_media_type(build_pdf("content")) == "application/pdf"

    def test_detects_docx(self) -> None:
        assert (
            sniff_media_type(build_docx("content"))
            == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )

    def test_detects_text(self) -> None:
        assert sniff_media_type(b"CHIEF COMPLAINT\nChest pain.") == "text/plain"

    def test_binary_payload_is_unrecognised(self) -> None:
        assert sniff_media_type(bytes([0x89, 0x50, 0x4E, 0x47, 0x00, 0x01])) is None


class TestUploadValidation:
    @pytest.fixture
    def settings(self) -> IngestionSettings:
        return IngestionSettings(max_upload_bytes=1024)

    def test_accepts_a_matching_declaration(self, settings: IngestionSettings) -> None:
        upload = validate_upload(
            b"CHIEF COMPLAINT\nChest pain.",
            declared_media_type="text/plain",
            filename="note.txt",
            settings=settings,
        )
        assert upload.media_type == "text/plain"
        assert upload.extension == ".txt"
        assert upload.size_bytes > 0

    def test_trusts_the_payload_over_a_generic_declaration(
        self, settings: IngestionSettings
    ) -> None:
        upload = validate_upload(
            build_pdf("x"),
            declared_media_type="application/octet-stream",
            filename="scan",
            settings=IngestionSettings(max_upload_bytes=10_000_000),
        )
        assert upload.media_type == "application/pdf"

    def test_rejects_a_mismatched_declaration(self, settings: IngestionSettings) -> None:
        """A mislabelled payload is a real problem, not something to silently correct."""
        with pytest.raises(ValidationFailedError, match="does not match"):
            validate_upload(
                b"just plain text",
                declared_media_type="application/pdf",
                filename="fake.pdf",
                settings=settings,
            )

    def test_rejects_an_empty_payload(self, settings: IngestionSettings) -> None:
        with pytest.raises(ValidationFailedError, match="empty"):
            validate_upload(b"", declared_media_type="text/plain", filename=None, settings=settings)

    def test_rejects_an_oversized_payload(self, settings: IngestionSettings) -> None:
        with pytest.raises(PayloadTooLargeError):
            validate_upload(
                b"x" * 2048,
                declared_media_type="text/plain",
                filename=None,
                settings=settings,
            )

    def test_rejects_an_unrecognisable_payload(self, settings: IngestionSettings) -> None:
        with pytest.raises(UnsupportedMediaTypeError):
            validate_upload(
                bytes([0x89, 0x50, 0x4E, 0x47, 0x00]),
                declared_media_type="application/octet-stream",
                filename="image.png",
                settings=settings,
            )

    def test_rejects_a_disallowed_media_type(self) -> None:
        settings = IngestionSettings(allowed_media_types=("application/pdf",))
        with pytest.raises(UnsupportedMediaTypeError, match="not accepted"):
            validate_upload(
                b"plain text content",
                declared_media_type="text/plain",
                filename="note.txt",
                settings=settings,
            )

    def test_media_type_parameters_are_ignored(self, settings: IngestionSettings) -> None:
        upload = validate_upload(
            b"content here",
            declared_media_type="text/plain; charset=utf-8",
            filename=None,
            settings=settings,
        )
        assert upload.media_type == "text/plain"

    def test_filename_is_sanitised(self, settings: IngestionSettings) -> None:
        upload = validate_upload(
            b"content",
            declared_media_type="text/plain",
            filename="../../etc/passwd",
            settings=settings,
        )
        assert upload.filename == "passwd"


class TestStructuredDocumentsAreNotPenalisedForSkippedChunking:
    """Regression cover: structured feeds were being quarantined for a non-failure.

    FHIR bundles and HL7v2 messages populate the relational and graph stores directly and
    are deliberately never chunk-embedded. The quality gate treated the resulting empty
    chunk set as a critical ``chunking`` failure, so every structured document was
    quarantined — silently withholding the highest-volume ingestion path in a hospital
    from retrieval.
    """

    @staticmethod
    def _assess(document_type: DocumentType):  # type: ignore[no-untyped-def]
        payload = b'{"resourceType": "Bundle", "entry": [{"resource": {"id": "1"}}]}'
        parsed = TextParser().parse(payload)
        normalized = normalize_document(parsed)
        metadata = DocumentMetadata(document_type=document_type)
        return assess_quality(parsed=parsed, normalized=normalized, chunks=(), metadata=metadata)

    @pytest.mark.parametrize(
        "document_type",
        [DocumentType.FHIR_BUNDLE, DocumentType.HL7V2_MESSAGE, DocumentType.DICOM_STUDY],
    )
    def test_structured_types_are_not_quarantined_for_having_no_chunks(
        self, document_type: DocumentType
    ) -> None:
        report = self._assess(document_type)
        assert report.verdict is not QualityVerdict.FAIL
        assert "chunking" not in {check.name for check in report.failed_checks}

    def test_the_chunking_check_records_why_it_was_skipped(self) -> None:
        report = self._assess(DocumentType.FHIR_BUNDLE)
        check = next(c for c in report.checks if c.name == "chunking")
        assert check.observed["chunking_expected"] is False
        assert "not applicable" in check.detail

    def test_narrative_types_still_fail_when_chunking_produces_nothing(self) -> None:
        """The check must keep catching a genuine chunking failure."""
        report = self._assess(DocumentType.DISCHARGE_SUMMARY)
        assert report.verdict is QualityVerdict.FAIL
        assert "chunking" in {check.name for check in report.failed_checks}

    def test_an_explicit_override_wins_over_the_document_type(self) -> None:
        parsed = TextParser().parse(b"Some narrative clinical content for the parser.")
        normalized = normalize_document(parsed)
        report = assess_quality(
            parsed=parsed,
            normalized=normalized,
            chunks=(),
            metadata=DocumentMetadata(document_type=DocumentType.DISCHARGE_SUMMARY),
            chunking_expected=False,
        )
        assert "chunking" not in {check.name for check in report.failed_checks}
