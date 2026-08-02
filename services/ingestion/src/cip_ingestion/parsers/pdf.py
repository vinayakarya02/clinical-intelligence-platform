"""PDF parser with OCR fallback.

Clinical PDFs arrive in three shapes and this parser has to handle all of them in one
document: born-digital pages with a real text layer, scanned pages with none, and hybrid
documents where a digital report has scanned attachments appended. The routing decision is
therefore made **per page**, not per document — a whole-document heuristic silently loses
the scanned appendix of an otherwise-digital report.

Layout awareness matters here beyond tidiness. ``pdfplumber`` exposes word positions, and
tables in clinical documents (lab panels, medication lists) lose their meaning when
flattened by naive left-to-right text extraction. Detected tables are preserved as
:attr:`BlockKind.TABLE` blocks so downstream chunking can keep them intact
(docs/architecture/02-rag-hybrid-retrieval.md §1.2).
"""

from __future__ import annotations

import re
from typing import Any

from cip_core.logging import get_logger
from cip_ingestion.domain import BlockKind, ParsedDocument, ParsedPage, TextBlock
from cip_ingestion.parsers.base import ParserError
from cip_ingestion.parsers.ocr import (
    NullOcrEngine,
    OcrEngine,
    OcrUnavailableError,
    page_needs_ocr,
    rasterize_pdf_page,
)

__all__ = ["PdfParser"]

_log = get_logger(__name__)

_PDF_MEDIA_TYPES = frozenset({"application/pdf"})

#: A heading in a clinical PDF is short, mostly uppercase, and often ends in a colon.
#: Deliberately conservative: a missed heading degrades to a paragraph, while a false
#: positive fragments a section and produces a chunk of one stray line.
_HEADING_PATTERN = re.compile(r"^[A-Z][A-Z0-9 ,\-/&()']{2,79}:?$")


class PdfParser:
    """Extracts text, layout blocks, and tables from PDF bytes."""

    def __init__(
        self,
        *,
        ocr_engine: OcrEngine | None = None,
        ocr_enabled: bool = True,
        ocr_dpi: int = 300,
        ocr_language: str = "eng",
        min_chars_per_page: int = 48,
    ) -> None:
        self._ocr_engine = ocr_engine or NullOcrEngine()
        self._ocr_enabled = ocr_enabled
        self._ocr_dpi = ocr_dpi
        self._ocr_language = ocr_language
        self._min_chars_per_page = min_chars_per_page

    @property
    def name(self) -> str:
        return "pdf"

    @property
    def supported_media_types(self) -> frozenset[str]:
        return _PDF_MEDIA_TYPES

    def parse(self, data: bytes, *, filename: str | None = None) -> ParsedDocument:
        import pdfplumber

        if not data:
            raise ParserError("PDF payload is empty", media_type="application/pdf")

        warnings: list[str] = []
        pages: list[ParsedPage] = []
        properties: dict[str, Any] = {}

        try:
            import io

            with pdfplumber.open(io.BytesIO(data)) as pdf:
                properties = self._extract_properties(pdf)
                for index, page in enumerate(pdf.pages, start=1):
                    parsed_page, page_warnings = self._parse_page(page, index, data)
                    pages.append(parsed_page)
                    warnings.extend(page_warnings)
        except ParserError:
            raise
        except Exception as exc:
            raise ParserError(
                f"PDF could not be opened: {type(exc).__name__}", media_type="application/pdf"
            ) from exc

        if not pages:
            raise ParserError("PDF contains no pages", media_type="application/pdf")

        return ParsedDocument(
            parser_name=self.name,
            media_type="application/pdf",
            pages=tuple(pages),
            properties=properties,
            warnings=tuple(warnings),
        )

    def _extract_properties(self, pdf: Any) -> dict[str, Any]:
        """Read the PDF ``/Info`` dictionary defensively.

        Producer-written metadata is frequently malformed or non-UTF-8; a bad title must
        not fail an otherwise-good document.
        """
        properties: dict[str, Any] = {"page_count": len(pdf.pages)}
        raw = getattr(pdf, "metadata", None) or {}
        for source_key, target_key in (
            ("Title", "title"),
            ("Author", "author"),
            ("Subject", "subject"),
            ("Producer", "producer"),
            ("CreationDate", "creation_date"),
        ):
            value = raw.get(source_key)
            if isinstance(value, str) and value.strip():
                properties[target_key] = value.strip()[:512]
        return properties

    def _parse_page(
        self, page: Any, page_number: int, raw_pdf: bytes
    ) -> tuple[ParsedPage, list[str]]:
        warnings: list[str] = []
        blocks: list[TextBlock] = []
        order = 0

        try:
            table_blocks, table_bboxes = self._extract_tables(page, page_number)
        except Exception as exc:
            table_blocks, table_bboxes = [], []
            warnings.append(f"page {page_number}: table extraction failed ({type(exc).__name__})")

        # Extract prose from the page with table regions removed, so table cell values do
        # not also appear as loose paragraph text and get chunked twice.
        try:
            prose_source = page
            for bbox in table_bboxes:
                prose_source = prose_source.outside_bbox(bbox)
            text = prose_source.extract_text() or ""
        except Exception as exc:
            text = ""
            warnings.append(f"page {page_number}: text extraction failed ({type(exc).__name__})")

        for line in self._split_blocks(text):
            kind = BlockKind.HEADING if _HEADING_PATTERN.match(line) else BlockKind.PARAGRAPH
            blocks.append(TextBlock(text=line, kind=kind, page_number=page_number, order=order))
            order += 1

        for table_block in table_blocks:
            blocks.append(
                TextBlock(
                    text=table_block,
                    kind=BlockKind.TABLE,
                    page_number=page_number,
                    order=order,
                )
            )
            order += 1

        native_chars = sum(len(block.text) for block in blocks)
        ocr_applied = False
        ocr_confidence: float | None = None

        if self._ocr_enabled and page_needs_ocr(native_chars, min_chars=self._min_chars_per_page):
            ocr_blocks, ocr_confidence, ocr_warning = self._ocr_page(page_number, raw_pdf, order)
            if ocr_warning:
                warnings.append(ocr_warning)
            if ocr_blocks:
                # Replace rather than append: native extraction produced almost nothing,
                # and mixing its fragments with OCR output duplicates partial words.
                blocks = ocr_blocks
                ocr_applied = True

        return (
            ParsedPage(
                page_number=page_number,
                blocks=tuple(blocks),
                ocr_applied=ocr_applied,
                ocr_confidence=ocr_confidence,
            ),
            warnings,
        )

    def _extract_tables(self, page: Any, page_number: int) -> tuple[list[str], list[tuple]]:
        """Extract tables as pipe-delimited text plus their bounding boxes.

        Pipe delimiting preserves cell boundaries that whitespace alone loses, which keeps
        "Sodium | 141 | mmol/L" readable as a row rather than an ambiguous run of numbers.
        """
        rendered: list[str] = []
        bboxes: list[tuple] = []
        for table in page.find_tables():
            rows = table.extract()
            if not rows:
                continue
            lines = [
                " | ".join((cell or "").strip().replace("\n", " ") for cell in row)
                for row in rows
                if any(cell and cell.strip() for cell in row)
            ]
            if lines:
                rendered.append("\n".join(lines))
                bboxes.append(table.bbox)
        if rendered:
            _log.debug("pdf.tables_extracted", page=page_number, count=len(rendered))
        return rendered, bboxes

    def _ocr_page(
        self, page_number: int, raw_pdf: bytes, start_order: int
    ) -> tuple[list[TextBlock], float | None, str | None]:
        if not self._ocr_engine.is_available():
            return (
                [],
                None,
                f"page {page_number}: appears scanned but OCR engine "
                f"'{self._ocr_engine.name}' is unavailable",
            )
        try:
            image = rasterize_pdf_page(raw_pdf, page_number, dpi=self._ocr_dpi)
            result = self._ocr_engine.recognize_page(image, language=self._ocr_language)
        except (OcrUnavailableError, ModuleNotFoundError) as exc:
            return ([], None, f"page {page_number}: OCR unavailable ({type(exc).__name__})")
        except Exception as exc:
            return ([], None, f"page {page_number}: OCR failed ({type(exc).__name__})")

        if not result.text.strip():
            return ([], result.confidence, f"page {page_number}: OCR produced no text")

        blocks = [
            TextBlock(
                text=line,
                kind=BlockKind.PARAGRAPH,
                page_number=page_number,
                order=start_order + offset,
                confidence=result.confidence,
            )
            for offset, line in enumerate(self._split_blocks(result.text))
        ]
        return blocks, result.confidence, None

    @staticmethod
    def _split_blocks(text: str) -> list[str]:
        """Split extracted text into non-empty, whitespace-collapsed lines."""
        return [stripped for line in text.splitlines() if (stripped := line.strip())]
