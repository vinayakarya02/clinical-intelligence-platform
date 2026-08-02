"""Clinical report normalisation.

Turns raw parser output into clean, offset-tracked text. Every transformation here exists
because it measurably damages retrieval if skipped:

* **Unicode NFC + ligature expansion** — PDF text layers emit ``ﬁ`` (U+FB01) and smart
  quotes. A keyword search for "fibrillation" does not match "ﬁbrillation", so the
  BM25 half of hybrid retrieval silently misses those documents.
* **De-hyphenation** — PDF line wrapping splits "hyper-\\ntension". Left alone, the
  clinical term is unsearchable and gets split across a chunk boundary.
* **Header/footer removal** — page furniture ("Patient: DOE, JOHN | MRN 12345 | Page 3 of 12")
  repeats on every page. Left in, it appears in every chunk, dominating short chunks and
  polluting them with PHI that has nothing to do with the surrounding content.
* **Page-number stripping** — bare numeric lines add noise and nothing else.

The output carries page offsets so any character position in the normalised text resolves
back to a source page, which is what makes chunk-level citation possible after the text
has been restructured.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass

from cip_ingestion.domain import BlockKind, NormalizedDocument, ParsedDocument, TextBlock

__all__ = ["NormalizationOptions", "normalize_document", "normalize_text"]

#: Ligatures and typographic characters that break exact-match retrieval.
_CHARACTER_REPLACEMENTS = {
    "ﬀ": "ff",
    "ﬁ": "fi",
    "ﬂ": "fl",
    "ﬃ": "ffi",
    "ﬄ": "ffl",
    "‘": "'",
    "’": "'",
    "“": '"',
    "”": '"',
    "–": "-",
    "—": "-",
    "−": "-",
    " ": " ",
    "​": "",
    "﻿": "",
}

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

#: Word broken across a line by PDF/print line-wrapping: "hyper-\ntension".
#:
#: The continuation must be lowercase. A hyphen before a capitalised word is far more
#: likely a genuine compound or an enumeration ("HIV-Positive", "COVID-19 Protocol") than
#: a wrap artefact, and rejoining those corrupts the term. ``\s*`` rather than ``[ \t]*``
#: because the parser emits each line as its own block and normalisation joins blocks with
#: a blank line, so the break the regex must span is "-\n\n", not "-\n".
_HYPHEN_LINEBREAK = re.compile(r"(\w)-[ \t]*\n\s*([a-z])")
_MULTI_SPACE = re.compile(r"[ \t]{2,}")
_MULTI_NEWLINE = re.compile(r"\n{3,}")
_PAGE_NUMBER_LINE = re.compile(
    r"^\s*(?:-\s*)?(?:page\s+)?\d{1,4}(?:\s*(?:of|/)\s*\d{1,4})?(?:\s*-)?\s*$", re.IGNORECASE
)


@dataclass(frozen=True, slots=True)
class NormalizationOptions:
    """Tunable normalisation behaviour."""

    strip_headers_footers: bool = True
    header_footer_min_page_ratio: float = 0.6
    """A line must repeat on at least this fraction of pages to count as furniture. 0.6
    tolerates the common case where a header is missing from the title page, while
    staying high enough that a clinical phrase recurring on a few pages is not stripped."""
    header_footer_max_chars: int = 120
    """Long lines are prose, not furniture, even when they repeat."""
    strip_page_numbers: bool = True
    dehyphenate: bool = True
    min_pages_for_furniture_detection: int = 3
    """Below this, "repeats on most pages" is not statistically meaningful — a two-page
    document with a shared line is more likely to be genuinely repeated content."""


def normalize_text(text: str, *, dehyphenate: bool = True) -> str:
    """Normalise a standalone string.

    Exposed separately from :func:`normalize_document` so section detection and chunking
    can normalise fragments without reconstructing a document.
    """
    normalized = unicodedata.normalize("NFC", text)
    for source, target in _CHARACTER_REPLACEMENTS.items():
        normalized = normalized.replace(source, target)
    normalized = _CONTROL_CHARS.sub("", normalized)
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
    if dehyphenate:
        normalized = _HYPHEN_LINEBREAK.sub(r"\1\2", normalized)
    normalized = _MULTI_SPACE.sub(" ", normalized)
    normalized = _MULTI_NEWLINE.sub("\n\n", normalized)
    return "\n".join(line.rstrip() for line in normalized.split("\n")).strip()


def _detect_furniture(parsed: ParsedDocument, options: NormalizationOptions) -> frozenset[str]:
    """Identify repeating header/footer lines across pages.

    Only the first and last few lines of each page are considered: a phrase repeating in
    the middle of several pages is clinical content (a recurring section heading), whereas
    furniture is positional by definition.
    """
    if (
        not options.strip_headers_footers
        or parsed.page_count < options.min_pages_for_furniture_detection
    ):
        return frozenset()

    counts: Counter[str] = Counter()
    for page in parsed.pages:
        lines = [block.text.strip() for block in page.blocks if block.text.strip()]
        candidates = lines[:2] + lines[-2:]
        for line in set(candidates):
            if 0 < len(line) <= options.header_footer_max_chars:
                counts[line] += 1

    threshold = max(2, int(parsed.page_count * options.header_footer_min_page_ratio))
    return frozenset(line for line, count in counts.items() if count >= threshold)


def _should_drop(line: str, furniture: frozenset[str], options: NormalizationOptions) -> bool:
    stripped = line.strip()
    if not stripped:
        return True
    if stripped in furniture:
        return True
    return bool(options.strip_page_numbers and _PAGE_NUMBER_LINE.match(stripped))


def normalize_document(
    parsed: ParsedDocument, options: NormalizationOptions | None = None
) -> NormalizedDocument:
    """Normalise a parsed document, preserving page offsets for citation.

    Table blocks are passed through with their internal line structure intact: collapsing
    a pipe-delimited lab panel into a paragraph destroys the row boundaries the parser
    worked to preserve.
    """
    options = options or NormalizationOptions()
    furniture = _detect_furniture(parsed, options)

    page_offsets: list[tuple[int, int, int]] = []
    segments: list[str] = []
    cursor = 0
    removed = 0

    for page in parsed.pages:
        page_lines: list[str] = []
        for block in page.blocks:
            block_text = _normalize_block(block, options)
            if not block_text:
                continue
            kept_lines: list[str] = []
            for line in block_text.split("\n"):
                if _should_drop(line, furniture, options):
                    removed += 1
                    continue
                kept_lines.append(line)
            if kept_lines:
                page_lines.append("\n".join(kept_lines))

        # De-hyphenate after joining, not per block: parsers emit one block per source
        # line, so the two halves of a wrapped word live in different blocks and the
        # break is only visible once they are adjacent. Doing this before offsets are
        # computed keeps the recorded character ranges consistent with the stored text.
        page_text = "\n\n".join(page_lines)
        if options.dehyphenate:
            page_text = _HYPHEN_LINEBREAK.sub(r"\1\2", page_text)

        if not page_text:
            # Record a zero-width span so page numbering stays aligned with reality; a
            # blank page must not shift subsequent pages' offsets.
            page_offsets.append((page.page_number, cursor, cursor))
            continue

        start = cursor
        segments.append(page_text)
        cursor += len(page_text)
        page_offsets.append((page.page_number, start, cursor))
        cursor += 2  # accounts for the "\n\n" joiner inserted between pages

    text = "\n\n".join(segments)
    # The final page's offset overshoots by the trailing joiner that is never emitted.
    if page_offsets:
        last_page, last_start, last_end = page_offsets[-1]
        page_offsets[-1] = (last_page, last_start, min(last_end, len(text)))

    return NormalizedDocument(
        text=text,
        page_offsets=tuple(page_offsets),
        removed_line_count=removed,
        source_char_count=parsed.char_count,
    )


def _normalize_block(block: TextBlock, options: NormalizationOptions) -> str:
    """Normalise a single block, preserving table row structure."""
    if block.kind is BlockKind.TABLE:
        rows = [
            normalize_text(row, dehyphenate=False) for row in block.text.split("\n") if row.strip()
        ]
        return "\n".join(row for row in rows if row)
    return normalize_text(block.text, dehyphenate=options.dehyphenate)
