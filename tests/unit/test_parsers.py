"""Parser tests, run against real generated PDF/DOCX/text payloads."""

from __future__ import annotations

import pytest

from cip_core.config import IngestionSettings
from cip_ingestion.domain import BlockKind
from cip_ingestion.parsers import (
    DocxParser,
    NullOcrEngine,
    ParserError,
    ParserRegistry,
    PdfParser,
    TextParser,
    build_parser_registry,
    page_needs_ocr,
)
from tests.fakes import StubOcrEngine
from tests.fixtures.documents import (
    DISCHARGE_SUMMARY_TEXT,
    LAB_REPORT_TEXT,
    build_docx,
    build_pdf,
    build_scanned_pdf,
)


class TestParserRegistry:
    def test_dispatches_by_media_type(self) -> None:
        registry = ParserRegistry()
        registry.register(TextParser())
        assert registry.get("text/plain").name == "text"

    def test_strips_media_type_parameters(self) -> None:
        registry = ParserRegistry()
        registry.register(TextParser())
        assert registry.get("text/plain; charset=utf-8").name == "text"

    def test_unknown_media_type_raises(self) -> None:
        with pytest.raises(ParserError, match="No parser registered"):
            ParserRegistry().get("application/zip")

    def test_conflicting_registration_is_refused(self) -> None:
        """Import-order-dependent parser selection is a bug, not a feature."""

        class Impostor(TextParser):
            @property
            def name(self) -> str:
                return "impostor"

        registry = ParserRegistry()
        registry.register(TextParser())
        with pytest.raises(ValueError, match="already handled"):
            registry.register(Impostor())

    def test_registering_the_same_parser_twice_is_idempotent(self) -> None:
        registry = ParserRegistry()
        parser = TextParser()
        registry.register(parser)
        registry.register(parser)
        assert registry.supports("text/plain")

    def test_default_registry_covers_the_supported_formats(self) -> None:
        registry = build_parser_registry(IngestionSettings(ocr_enabled=False))
        assert registry.media_types == frozenset(IngestionSettings().allowed_media_types)


class TestTextParser:
    def test_parses_utf8(self) -> None:
        parsed = TextParser().parse(DISCHARGE_SUMMARY_TEXT.encode("utf-8"))
        assert parsed.parser_name == "text"
        assert parsed.page_count == 1
        assert "CHIEF COMPLAINT" in parsed.text
        assert parsed.properties["encoding"] == "utf-8"

    def test_detects_uppercase_headings(self) -> None:
        parsed = TextParser().parse(b"CHIEF COMPLAINT\nChest pain reported by the patient.")
        kinds = [block.kind for block in parsed.pages[0].blocks]
        assert kinds[0] is BlockKind.HEADING
        assert kinds[1] is BlockKind.PARAGRAPH

    def test_falls_back_to_cp1252_and_warns(self) -> None:
        """Legacy Windows exports must be ingested, with the fallback recorded."""
        payload = "Temperature 37.5°C – stable".encode("cp1252")
        parsed = TextParser().parse(payload)
        assert parsed.properties["encoding"] != "utf-8"
        assert any("fallback encoding" in warning for warning in parsed.warnings)

    def test_empty_payload_raises(self) -> None:
        with pytest.raises(ParserError, match="empty"):
            TextParser().parse(b"")

    def test_whitespace_only_payload_raises(self) -> None:
        with pytest.raises(ParserError, match="no readable content"):
            TextParser().parse(b"   \n\n   \t\n")


class TestPdfParser:
    def _parser(self, **kwargs: object) -> PdfParser:
        defaults: dict[str, object] = {"ocr_enabled": False, "ocr_engine": NullOcrEngine()}
        defaults.update(kwargs)
        return PdfParser(**defaults)  # type: ignore[arg-type]

    def test_extracts_text_from_a_born_digital_pdf(self) -> None:
        parsed = self._parser().parse(build_pdf(DISCHARGE_SUMMARY_TEXT))
        assert parsed.parser_name == "pdf"
        assert parsed.page_count >= 1
        assert "CHIEF COMPLAINT" in parsed.text
        assert parsed.ocr_page_count == 0

    def test_reads_document_properties(self) -> None:
        parsed = self._parser().parse(build_pdf("Body text here.", title="Discharge Summary"))
        assert parsed.properties.get("title") == "Discharge Summary"
        assert parsed.properties["page_count"] == parsed.page_count

    def test_multi_page_documents_preserve_page_numbers(self) -> None:
        parsed = self._parser().parse(build_pdf("Line\n" * 200))
        assert parsed.page_count > 1
        assert [page.page_number for page in parsed.pages] == list(range(1, parsed.page_count + 1))

    def test_rejects_a_non_pdf_payload(self) -> None:
        with pytest.raises(ParserError):
            self._parser().parse(b"this is definitely not a pdf")

    def test_rejects_an_empty_payload(self) -> None:
        with pytest.raises(ParserError, match="empty"):
            self._parser().parse(b"")

    def test_scanned_page_without_ocr_records_a_warning(self) -> None:
        """A scanned page must never be silently dropped."""
        parser = self._parser(ocr_enabled=True, ocr_engine=StubOcrEngine(available=False))
        parsed = parser.parse(build_scanned_pdf())
        assert parsed.ocr_page_count == 0
        assert any("OCR engine" in warning for warning in parsed.warnings)

    def test_scanned_page_is_recovered_by_ocr(self) -> None:
        engine = StubOcrEngine(text="OCR RECOVERED TEXT\nScanned content.", confidence=0.88)
        parser = self._parser(ocr_enabled=True, ocr_engine=engine)
        parsed = parser.parse(build_scanned_pdf())

        if engine.calls == 0:
            pytest.skip("PDF rasterisation requires Poppler, which is not installed")

        assert parsed.ocr_page_count == 1
        assert "OCR RECOVERED TEXT" in parsed.text
        assert parsed.mean_ocr_confidence == pytest.approx(0.88)

    def test_ocr_is_not_attempted_when_a_text_layer_exists(self) -> None:
        engine = StubOcrEngine()
        parser = self._parser(ocr_enabled=True, ocr_engine=engine)
        parser.parse(build_pdf(DISCHARGE_SUMMARY_TEXT))
        assert engine.calls == 0, "OCR must not run on pages with extractable text"


class TestDocxParser:
    def test_extracts_text_headings_and_tables(self) -> None:
        parsed = DocxParser().parse(build_docx(DISCHARGE_SUMMARY_TEXT))
        assert parsed.parser_name == "docx"
        assert "CHIEF COMPLAINT" in parsed.text

        kinds = {block.kind for block in parsed.pages[0].blocks}
        assert BlockKind.HEADING in kinds
        assert BlockKind.TABLE in kinds

    def test_tables_are_pipe_delimited(self) -> None:
        parsed = DocxParser().parse(build_docx(LAB_REPORT_TEXT))
        tables = [b.text for b in parsed.pages[0].blocks if b.kind is BlockKind.TABLE]
        assert tables
        assert "|" in tables[0]

    def test_reads_core_properties(self) -> None:
        parsed = DocxParser().parse(
            build_docx("Body.", title="Discharge Summary", author="Dr Chen")
        )
        assert parsed.properties["title"] == "Discharge Summary"
        assert parsed.properties["author"] == "Dr Chen"

    def test_body_order_is_preserved(self) -> None:
        """A table must stay adjacent to the paragraph that introduces it."""
        parsed = DocxParser().parse(build_docx("MEDICATIONS\nLisinopril | 10 mg\nEND NOTE"))
        kinds = [block.kind for block in parsed.pages[0].blocks]
        assert kinds.index(BlockKind.TABLE) > kinds.index(BlockKind.HEADING)

    def test_docx_has_a_single_logical_page(self) -> None:
        """DOCX has no page concept; inventing page numbers would fabricate citations."""
        parsed = DocxParser().parse(build_docx(DISCHARGE_SUMMARY_TEXT))
        assert parsed.page_count == 1

    def test_rejects_a_non_docx_payload(self) -> None:
        with pytest.raises(ParserError):
            DocxParser().parse(b"PK\x03\x04 not really a docx")

    def test_rejects_an_empty_payload(self) -> None:
        with pytest.raises(ParserError, match="empty"):
            DocxParser().parse(b"")


class TestOcrRouting:
    @pytest.mark.parametrize(
        ("chars", "expected"),
        [(0, True), (10, True), (47, True), (48, False), (5000, False)],
    )
    def test_threshold_behaviour(self, chars: int, expected: bool) -> None:
        assert page_needs_ocr(chars, min_chars=48) is expected

    def test_null_engine_reports_unavailable(self) -> None:
        assert NullOcrEngine().is_available() is False
