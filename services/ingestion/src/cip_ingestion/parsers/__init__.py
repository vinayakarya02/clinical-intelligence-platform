"""Document parsers and the registry that dispatches to them."""

from __future__ import annotations

from cip_core.config import IngestionSettings
from cip_ingestion.parsers.base import DocumentParser, ParserError, ParserRegistry
from cip_ingestion.parsers.docx import DocxParser
from cip_ingestion.parsers.ocr import (
    NullOcrEngine,
    OcrEngine,
    OcrPageResult,
    OcrUnavailableError,
    TesseractOcrEngine,
    page_needs_ocr,
)
from cip_ingestion.parsers.pdf import PdfParser
from cip_ingestion.parsers.text import TextParser

__all__ = [
    "DocumentParser",
    "DocxParser",
    "NullOcrEngine",
    "OcrEngine",
    "OcrPageResult",
    "OcrUnavailableError",
    "ParserError",
    "ParserRegistry",
    "PdfParser",
    "TesseractOcrEngine",
    "TextParser",
    "build_parser_registry",
    "page_needs_ocr",
]


def build_parser_registry(
    settings: IngestionSettings, *, ocr_engine: OcrEngine | None = None
) -> ParserRegistry:
    """Construct the registry for the configured pipeline.

    ``ocr_engine`` is injectable so tests can supply a deterministic engine, and so a
    deployment can substitute a managed OCR service without touching the parsers. When
    OCR is disabled, a :class:`NullOcrEngine` is wired in rather than ``None``, keeping
    the PDF parser free of null checks.
    """
    engine = ocr_engine or (TesseractOcrEngine() if settings.ocr_enabled else NullOcrEngine())

    registry = ParserRegistry()
    registry.register(
        PdfParser(
            ocr_engine=engine,
            ocr_enabled=settings.ocr_enabled,
            ocr_dpi=settings.ocr_dpi,
            ocr_language=settings.ocr_language,
            min_chars_per_page=settings.ocr_min_text_chars_per_page,
        )
    )
    registry.register(DocxParser())
    registry.register(TextParser())
    return registry
