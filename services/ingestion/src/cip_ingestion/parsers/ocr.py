"""OCR pipeline.

Scanned clinical documents — faxed referrals, signed consent forms, older records — carry
no text layer, and dropping them silently is the worst available outcome: the document
appears ingested, the pipeline reports success, and its content is simply absent from
every future search.

The design therefore separates three concerns:

* :class:`OcrEngine` — the OCR contract. A protocol, so tests inject a deterministic
  double instead of depending on a binary, and a future managed OCR service (AWS Textract,
  Azure Document Intelligence) is a new implementation rather than a rewrite.
* :class:`TesseractOcrEngine` — the Phase 1 implementation, which reports its own
  availability instead of failing at first use.
* :func:`page_needs_ocr` — the routing decision, kept pure and independently testable
  because it is where a wrong threshold silently loses clinical content.

Availability is checked, not assumed. Tesseract is a system binary that Python packaging
cannot install, so the engine answers :meth:`is_available` and the pipeline degrades
loudly (a quality-report warning) rather than crashing when it is missing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from cip_core.logging import get_logger

__all__ = [
    "NullOcrEngine",
    "OcrEngine",
    "OcrPageResult",
    "OcrUnavailableError",
    "TesseractOcrEngine",
    "page_needs_ocr",
]

_log = get_logger(__name__)


class OcrUnavailableError(RuntimeError):
    """Raised when OCR is required but no working engine is installed."""


@dataclass(frozen=True, slots=True)
class OcrPageResult:
    """Text recovered from one rasterised page."""

    text: str
    confidence: float | None
    """Mean per-word confidence in [0, 1], or ``None`` if the engine does not report it."""
    engine: str


def page_needs_ocr(extracted_char_count: int, *, min_chars: int) -> bool:
    """Decide whether a page's native text extraction is too sparse to trust.

    The threshold is deliberately generous (see ``ocr_min_text_chars_per_page``): a false
    positive costs OCR time on a mostly-empty page, while a false negative permanently
    drops a scanned page's content from retrieval. Those costs are not symmetric.
    """
    return extracted_char_count < min_chars


@runtime_checkable
class OcrEngine(Protocol):
    """Optical character recognition over rasterised page images."""

    @property
    def name(self) -> str: ...

    def is_available(self) -> bool:
        """Whether this engine can actually run. Must not raise."""
        ...

    def recognize_page(self, image: Any, *, language: str) -> OcrPageResult:
        """Recognise text in a single PIL image."""
        ...


class NullOcrEngine:
    """Engine used when OCR is disabled by configuration.

    Distinct from "no engine installed": this reports unavailability without probing the
    system, so a deployment that intentionally runs OCR-free does not log missing-binary
    warnings on every scanned page.
    """

    @property
    def name(self) -> str:
        return "null"

    def is_available(self) -> bool:
        return False

    def recognize_page(self, image: Any, *, language: str) -> OcrPageResult:
        raise OcrUnavailableError("OCR is disabled by configuration")


class TesseractOcrEngine:
    """Tesseract-backed OCR."""

    def __init__(self, *, tesseract_cmd: str | None = None) -> None:
        self._tesseract_cmd = tesseract_cmd
        self._available: bool | None = None

    @property
    def name(self) -> str:
        return "tesseract"

    def _probe(self) -> bool:
        """Probe once per instance; the binary does not appear or vanish mid-process."""
        try:
            import pytesseract
        except ModuleNotFoundError:
            _log.warning("ocr.unavailable", reason="pytesseract_not_installed")
            return False
        if self._tesseract_cmd:
            pytesseract.pytesseract.tesseract_cmd = self._tesseract_cmd
        try:
            version = pytesseract.get_tesseract_version()
        except Exception as exc:
            _log.warning("ocr.unavailable", reason=type(exc).__name__)
            return False
        _log.info("ocr.available", engine=self.name, version=str(version))
        return True

    def is_available(self) -> bool:
        if self._available is None:
            self._available = self._probe()
        return self._available

    def recognize_page(self, image: Any, *, language: str) -> OcrPageResult:
        if not self.is_available():
            raise OcrUnavailableError("Tesseract is not available on this host")
        import pytesseract

        data = pytesseract.image_to_data(image, lang=language, output_type=pytesseract.Output.DICT)
        words: list[str] = []
        confidences: list[float] = []
        for text, raw_conf in zip(data.get("text", []), data.get("conf", []), strict=False):
            token = (text or "").strip()
            if not token:
                continue
            words.append(token)
            try:
                conf = float(raw_conf)
            except (TypeError, ValueError):
                continue
            # Tesseract reports -1 for tokens it declines to score; averaging those in
            # would drag confidence below any sane threshold and quarantine good pages.
            if conf >= 0:
                confidences.append(conf / 100.0)

        return OcrPageResult(
            text=" ".join(words),
            confidence=sum(confidences) / len(confidences) if confidences else None,
            engine=self.name,
        )


def rasterize_pdf_page(data: bytes, page_number: int, *, dpi: int) -> Any:
    """Render one 1-indexed PDF page to a PIL image for OCR.

    Isolated here because it depends on Poppler via ``pdf2image`` — another system binary
    the PDF parser should not have to know about.
    """
    from pdf2image import convert_from_bytes

    images = convert_from_bytes(data, dpi=dpi, first_page=page_number, last_page=page_number)
    if not images:
        raise OcrUnavailableError(f"Failed to rasterise page {page_number}")
    return images[0]
