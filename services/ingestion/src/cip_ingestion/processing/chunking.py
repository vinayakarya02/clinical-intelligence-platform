"""Chunking.

Phase 0 specifies embedding-similarity breakpoint detection as the chunking strategy
(docs/architecture/02-rag-hybrid-retrieval.md §1.2). Phase 1 explicitly excludes
embeddings, so that algorithm cannot be implemented yet. Rather than approximate it badly
or block on Phase 2, chunking sits behind the :class:`Chunker` protocol with a
structural implementation now and the embedding-based one as a later implementation of the
same interface. ADR-0006 records this.

:class:`StructuralSemanticChunker` uses the structure the document already carries —
clinical section boundaries, paragraph breaks, sentence boundaries — as its semantic
signal. For clinical documents this is a genuinely strong proxy: sections are topical by
construction, which is exactly what embedding-similarity breakpoints try to recover.

Invariants the implementation guarantees, each enforced by a test:

1. Chunks never cross a section boundary.
2. Table blocks are never split — a fragment of a lab panel is not a usable retrieval unit.
3. Chunks never exceed ``chunk_max_tokens``, except for a single indivisible unit that
   exceeds it alone, which is emitted whole rather than truncated.
4. Character ranges are exact, so every chunk can be traced back to its source span.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from cip_core.config import IngestionSettings
from cip_core.models.enums import SectionType
from cip_ingestion.domain import DetectedSection, NormalizedDocument, TextChunk
from cip_ingestion.processing.tokenization import (
    HeuristicTokenEstimator,
    TokenEstimator,
    split_sentences,
)

__all__ = ["Chunker", "ChunkingOptions", "StructuralSemanticChunker"]


@dataclass(frozen=True, slots=True)
class ChunkingOptions:
    """Chunk sizing policy."""

    target_tokens: int = 384
    min_tokens: int = 64
    max_tokens: int = 512
    overlap_ratio: float = 0.12

    @classmethod
    def from_settings(cls, settings: IngestionSettings) -> ChunkingOptions:
        return cls(
            target_tokens=settings.chunk_target_tokens,
            min_tokens=settings.chunk_min_tokens,
            max_tokens=settings.chunk_max_tokens,
            overlap_ratio=settings.chunk_overlap_ratio,
        )

    def __post_init__(self) -> None:
        if not 0 < self.min_tokens <= self.target_tokens <= self.max_tokens:
            raise ValueError("Require 0 < min_tokens <= target_tokens <= max_tokens")
        if not 0.0 <= self.overlap_ratio < 0.5:
            raise ValueError("overlap_ratio must be in [0.0, 0.5)")


@runtime_checkable
class Chunker(Protocol):
    """Splits a normalised document into retrieval units."""

    @property
    def name(self) -> str: ...

    def chunk(self, document: NormalizedDocument) -> tuple[TextChunk, ...]: ...


@dataclass(frozen=True, slots=True)
class _Unit:
    """An indivisible span considered for chunk assembly."""

    start: int
    end: int
    tokens: int
    is_atomic: bool = False
    """Atomic units (table rows belonging to one table) are never split further."""


class StructuralSemanticChunker:
    """Section-aware, sentence-boundary-respecting chunker."""

    def __init__(
        self,
        options: ChunkingOptions | None = None,
        *,
        token_estimator: TokenEstimator | None = None,
    ) -> None:
        self._options = options or ChunkingOptions()
        self._tokens = token_estimator or HeuristicTokenEstimator()

    @property
    def name(self) -> str:
        return f"structural-semantic/{self._tokens.name}"

    def chunk(self, document: NormalizedDocument) -> tuple[TextChunk, ...]:
        if not document.text.strip():
            return ()

        sections = document.sections or (
            DetectedSection(
                heading=None,
                section_type=SectionType.NARRATIVE,
                canonical_name="document_body",
                char_start=0,
                char_end=len(document.text),
            ),
        )

        chunks: list[TextChunk] = []
        for section in sections:
            for span_start, span_end in self._assemble(document.text, section):
                text = document.text[span_start:span_end].strip()
                if not text:
                    continue
                # Re-derive exact offsets after stripping so the recorded range matches
                # the stored text rather than the pre-strip span.
                leading = len(document.text[span_start:span_end]) - len(
                    document.text[span_start:span_end].lstrip()
                )
                start = span_start + leading
                end = start + len(text)
                chunks.append(
                    TextChunk(
                        index=len(chunks),
                        text=text,
                        char_start=start,
                        char_end=end,
                        token_count=self._tokens.count(text),
                        section_type=section.section_type,
                        section_heading=section.heading,
                        page_start=document.page_for_offset(start),
                        page_end=document.page_for_offset(max(start, end - 1)),
                        metadata={
                            "section_name": section.canonical_name,
                            "section_confidence": section.confidence,
                            "chunker": self.name,
                        },
                    )
                )
        return tuple(chunks)

    def _assemble(self, text: str, section: DetectedSection) -> list[tuple[int, int]]:
        """Group a section's units into chunk spans."""
        units = self._build_units(text, section)
        if not units:
            return []

        spans: list[tuple[int, int]] = []
        current: list[_Unit] = []
        current_tokens = 0

        for unit in units:
            would_exceed = current_tokens + unit.tokens > self._options.max_tokens
            if current and would_exceed:
                spans.append((current[0].start, current[-1].end))
                current = self._carry_overlap(current)
                current_tokens = sum(item.tokens for item in current)

            current.append(unit)
            current_tokens += unit.tokens

            # Close the chunk once it reaches target size, leaving headroom rather than
            # packing to the hard maximum — a chunk at exactly max_tokens has no room for
            # the metadata header a retriever may prepend at query time.
            if current_tokens >= self._options.target_tokens:
                spans.append((current[0].start, current[-1].end))
                current = self._carry_overlap(current)
                current_tokens = sum(item.tokens for item in current)

        if current:
            tail = (current[0].start, current[-1].end)
            # Merge an undersized trailing chunk back into its predecessor when they are
            # adjacent, so a section does not end with a 12-token orphan that carries no
            # retrievable meaning on its own.
            if (
                spans
                and current_tokens < self._options.min_tokens
                and spans[-1][1] <= tail[0]
                and self._tokens.count(text[spans[-1][0] : tail[1]]) <= self._options.max_tokens
            ):
                spans[-1] = (spans[-1][0], tail[1])
            else:
                spans.append(tail)

        return self._deduplicate(spans)

    def _build_units(self, text: str, section: DetectedSection) -> list[_Unit]:
        """Split a section into indivisible units.

        Table blocks become a single atomic unit; prose splits at sentence boundaries.
        """
        units: list[_Unit] = []
        for block_start, block_end, is_table in self._iter_blocks(text, section):
            block_text = text[block_start:block_end]
            if not block_text.strip():
                continue

            if is_table:
                units.append(
                    _Unit(
                        start=block_start,
                        end=block_end,
                        tokens=self._tokens.count(block_text),
                        is_atomic=True,
                    )
                )
                continue

            sentence_spans = split_sentences(block_text)
            if not sentence_spans:
                units.append(_Unit(block_start, block_end, self._tokens.count(block_text)))
                continue
            for rel_start, rel_end in sentence_spans:
                fragment = block_text[rel_start:rel_end]
                if fragment.strip():
                    units.append(
                        _Unit(
                            start=block_start + rel_start,
                            end=block_start + rel_end,
                            tokens=self._tokens.count(fragment),
                        )
                    )
        return units

    @staticmethod
    def _iter_blocks(text: str, section: DetectedSection):  # type: ignore[no-untyped-def]
        """Yield ``(start, end, is_table)`` blocks within a section.

        A run of consecutive pipe-delimited lines is treated as one table: the parsers
        render tables that way, and grouping the run keeps a lab panel's rows together
        through chunk assembly.
        """
        offset = section.char_start
        block_start = offset
        block_is_table: bool | None = None

        for line in text[section.char_start : section.char_end].split("\n"):
            line_end = offset + len(line)
            is_table_line = "|" in line and line.strip() != ""

            if block_is_table is None:
                block_is_table = is_table_line
            elif is_table_line != block_is_table:
                if block_start < offset:
                    yield block_start, offset, block_is_table
                block_start = offset
                block_is_table = is_table_line

            offset = line_end + 1

        final_end = min(offset, section.char_end)
        if block_start < final_end:
            yield block_start, final_end, bool(block_is_table)

    def _carry_overlap(self, units: list[_Unit]) -> list[_Unit]:
        """Select trailing units to repeat at the head of the next chunk.

        Overlap preserves context across a boundary so a sentence answering a query is not
        stranded without its lead-in. Atomic table units are never carried: duplicating a
        whole lab panel into the following chunk wastes most of its budget.
        """
        if self._options.overlap_ratio <= 0.0:
            return []

        budget = int(self._options.target_tokens * self._options.overlap_ratio)
        if budget <= 0:
            return []

        carried: list[_Unit] = []
        total = 0
        for unit in reversed(units):
            if unit.is_atomic or total + unit.tokens > budget:
                break
            carried.insert(0, unit)
            total += unit.tokens
        return carried

    @staticmethod
    def _deduplicate(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
        """Drop empty and fully-contained spans produced by overlap carry-over."""
        result: list[tuple[int, int]] = []
        for start, end in spans:
            if end <= start:
                continue
            if result and start >= result[-1][0] and end <= result[-1][1]:
                continue
            result.append((start, end))
        return result
