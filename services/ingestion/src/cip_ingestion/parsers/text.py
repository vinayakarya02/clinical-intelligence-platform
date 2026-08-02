"""Plain-text parser.

Simple, but not trivial: clinical text arrives from legacy systems in whatever encoding
the source used, and a mojibake'd document is worse than a rejected one because it is
ingested, indexed, and silently unsearchable. The parser therefore decodes through an
explicit, ordered candidate list and records which encoding won, so a corpus-wide encoding
problem is visible in metadata rather than buried in the text.
"""

from __future__ import annotations

import re
from typing import Any

from cip_ingestion.domain import BlockKind, ParsedDocument, ParsedPage, TextBlock
from cip_ingestion.parsers.base import ParserError

__all__ = ["TextParser"]

_TEXT_MEDIA_TYPES = frozenset({"text/plain"})

#: Ordered decode candidates. UTF-8 first (correct for anything modern); cp1252 next
#: because Windows-authored clinical exports dominate the legacy corpus; latin-1 last as a
#: total function that never raises, guaranteeing the document is ingested rather than lost.
_ENCODING_CANDIDATES = ("utf-8", "utf-8-sig", "cp1252", "latin-1")

_HEADING_PATTERN = re.compile(r"^[A-Z][A-Z0-9 ,\-/&()']{2,79}:?$")


class TextParser:
    """Parses plain-text documents into blocks."""

    @property
    def name(self) -> str:
        return "text"

    @property
    def supported_media_types(self) -> frozenset[str]:
        return _TEXT_MEDIA_TYPES

    def parse(self, data: bytes, *, filename: str | None = None) -> ParsedDocument:
        if not data:
            raise ParserError("Text payload is empty", media_type="text/plain")

        text, encoding, replacement_count = self._decode(data)
        warnings: list[str] = []
        if encoding != "utf-8":
            warnings.append(f"decoded using fallback encoding '{encoding}'")
        if replacement_count:
            warnings.append(f"{replacement_count} undecodable byte(s) replaced")

        blocks: list[TextBlock] = []
        for order, line in enumerate(
            stripped for raw in text.splitlines() if (stripped := raw.strip())
        ):
            kind = BlockKind.HEADING if _HEADING_PATTERN.match(line) else BlockKind.PARAGRAPH
            blocks.append(TextBlock(text=line, kind=kind, page_number=1, order=order))

        if not blocks:
            raise ParserError("Text payload contains no readable content", media_type="text/plain")

        properties: dict[str, Any] = {"encoding": encoding, "byte_length": len(data)}
        return ParsedDocument(
            parser_name=self.name,
            media_type="text/plain",
            pages=(ParsedPage(page_number=1, blocks=tuple(blocks)),),
            properties=properties,
            warnings=tuple(warnings),
        )

    @staticmethod
    def _decode(data: bytes) -> tuple[str, str, int]:
        """Decode bytes, returning ``(text, encoding_used, replacement_count)``."""
        for encoding in _ENCODING_CANDIDATES:
            try:
                return data.decode(encoding), encoding, 0
            except UnicodeDecodeError:
                continue
        # Unreachable in practice — latin-1 maps every byte — but a lossy decode is still
        # preferable to dropping a clinical document, so the fallback is explicit.
        text = data.decode("utf-8", errors="replace")
        return text, "utf-8-replace", text.count("�")
