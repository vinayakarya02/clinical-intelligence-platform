"""Domain value objects for the document-intelligence pipeline.

Every stage of the pipeline consumes and produces these types rather than dictionaries.
That is what makes the stages independently testable: a chunking test constructs a
``NormalizedDocument`` directly and never touches a PDF, and a parser test asserts on a
``ParsedDocument`` without a database.

All types are frozen dataclasses. A pipeline stage produces a *new* value rather than
mutating its input, so a failure mid-pipeline leaves no partially-mutated state and the
input to any stage can be replayed for debugging.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from cip_core.models.enums import DocumentType, SectionType

__all__ = [
    "BlockKind",
    "DetectedSection",
    "DocumentMetadata",
    "NormalizedDocument",
    "ParsedDocument",
    "ParsedPage",
    "TextBlock",
    "TextChunk",
    "content_digest",
]


def content_digest(text: str) -> str:
    """Stable SHA-256 of text content.

    Normalising to NFC before hashing means two encodings of the same characters produce
    one hash, so duplicate detection is not defeated by an upstream system that emits
    decomposed Unicode.
    """
    import unicodedata

    return hashlib.sha256(unicodedata.normalize("NFC", text).encode("utf-8")).hexdigest()


class BlockKind(StrEnum):
    """What a parser believes a block of text is.

    Kept coarse on purpose: parsers can distinguish these reliably across PDF/DOCX/OCR,
    whereas finer classification (list item vs. caption vs. footnote) is not reliably
    recoverable and would give downstream stages false confidence.
    """

    PARAGRAPH = "paragraph"
    HEADING = "heading"
    TABLE = "table"
    LIST_ITEM = "list_item"
    HEADER_FOOTER = "header_footer"
    """Repeating page furniture. Identified during normalisation and excluded from
    chunking — leaving it in pollutes every chunk with the hospital's letterhead."""


@dataclass(frozen=True, slots=True)
class TextBlock:
    """A contiguous run of text with provenance back to its page."""

    text: str
    kind: BlockKind = BlockKind.PARAGRAPH
    page_number: int = 1
    order: int = 0
    confidence: float | None = None
    """OCR confidence in [0, 1]; ``None`` for natively extracted text."""

    def with_text(self, text: str) -> TextBlock:
        return TextBlock(
            text=text,
            kind=self.kind,
            page_number=self.page_number,
            order=self.order,
            confidence=self.confidence,
        )


@dataclass(frozen=True, slots=True)
class ParsedPage:
    """One page of a parsed document."""

    page_number: int
    blocks: tuple[TextBlock, ...] = ()
    ocr_applied: bool = False
    ocr_confidence: float | None = None

    @property
    def text(self) -> str:
        return "\n".join(block.text for block in self.blocks if block.text)

    @property
    def char_count(self) -> int:
        return len(self.text)


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    """Raw parser output, before normalisation.

    This is the artifact persisted to MongoDB: nested, format-dependent, written once and
    read whole (ADR-0005).
    """

    parser_name: str
    media_type: str
    pages: tuple[ParsedPage, ...] = ()
    properties: dict[str, Any] = field(default_factory=dict)
    """Format-native properties (PDF ``/Info`` dictionary, DOCX core properties)."""
    warnings: tuple[str, ...] = ()
    """Non-fatal parse problems. Surfaced in the quality report rather than raised, so a
    partially-degraded document is still ingested with its degradation recorded."""

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def text(self) -> str:
        return "\n\n".join(page.text for page in self.pages)

    @property
    def char_count(self) -> int:
        return sum(page.char_count for page in self.pages)

    @property
    def ocr_page_count(self) -> int:
        return sum(1 for page in self.pages if page.ocr_applied)

    @property
    def mean_ocr_confidence(self) -> float | None:
        values = [p.ocr_confidence for p in self.pages if p.ocr_confidence is not None]
        return sum(values) / len(values) if values else None


@dataclass(frozen=True, slots=True)
class DetectedSection:
    """A clinical section located within normalised text.

    Character offsets index into :attr:`NormalizedDocument.text`, so a chunk can be traced
    to its section and back to the exact source span.
    """

    heading: str | None
    section_type: SectionType
    canonical_name: str
    char_start: int
    char_end: int
    page_start: int | None = None
    page_end: int | None = None
    confidence: float = 1.0

    @property
    def length(self) -> int:
        return self.char_end - self.char_start


@dataclass(frozen=True, slots=True)
class DocumentMetadata:
    """Metadata extracted from content and format properties."""

    title: str | None = None
    author: str | None = None
    document_type: DocumentType = DocumentType.UNKNOWN
    document_type_confidence: float = 0.0
    effective_date: dt.date | None = None
    language: str | None = None
    page_count: int = 0
    word_count: int = 0
    section_names: tuple[str, ...] = ()
    phi_indicators: tuple[str, ...] = ()
    """Categories of potential PHI detected (``mrn``, ``ssn``, ``phone``, ...). Category
    names only — never the matched values, which would move PHI into a metadata column
    that is not treated as PHI."""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "author": self.author,
            "document_type": str(self.document_type),
            "document_type_confidence": round(self.document_type_confidence, 4),
            "effective_date": self.effective_date.isoformat() if self.effective_date else None,
            "language": self.language,
            "page_count": self.page_count,
            "word_count": self.word_count,
            "section_names": list(self.section_names),
            "phi_indicators": list(self.phi_indicators),
            **self.extra,
        }


@dataclass(frozen=True, slots=True)
class NormalizedDocument:
    """Cleaned document text with section structure and page offsets."""

    text: str
    sections: tuple[DetectedSection, ...] = ()
    page_offsets: tuple[tuple[int, int, int], ...] = ()
    """``(page_number, char_start, char_end)`` triples, so a character offset in
    :attr:`text` can be resolved back to a page for citation."""
    removed_line_count: int = 0
    source_char_count: int = 0

    @property
    def char_count(self) -> int:
        return len(self.text)

    def page_for_offset(self, offset: int) -> int | None:
        """Resolve a character offset to its page number."""
        for page_number, start, end in self.page_offsets:
            if start <= offset < end:
                return page_number
        return self.page_offsets[-1][0] if self.page_offsets else None

    def section_for_offset(self, offset: int) -> DetectedSection | None:
        for section in self.sections:
            if section.char_start <= offset < section.char_end:
                return section
        return None


@dataclass(frozen=True, slots=True)
class TextChunk:
    """A retrieval-unit chunk, ready for persistence."""

    index: int
    text: str
    char_start: int
    char_end: int
    token_count: int
    section_type: SectionType = SectionType.NARRATIVE
    section_heading: str | None = None
    page_start: int | None = None
    page_end: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def content_hash(self) -> str:
        return content_digest(self.text)

    @property
    def char_count(self) -> int:
        return len(self.text)
