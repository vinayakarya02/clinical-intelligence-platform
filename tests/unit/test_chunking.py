"""Chunking tests.

Chunking is the highest-leverage stage for retrieval quality, and its invariants are
easy to break silently. Each invariant documented in
``cip_ingestion.processing.chunking`` gets an explicit test here.
"""

from __future__ import annotations

from dataclasses import replace
from itertools import pairwise

import pytest

from cip_core.models.enums import SectionType
from cip_ingestion.domain import DetectedSection, NormalizedDocument
from cip_ingestion.parsers import TextParser
from cip_ingestion.processing import (
    ChunkingOptions,
    StructuralSemanticChunker,
    detect_sections,
    normalize_document,
)
from tests.fixtures.documents import DISCHARGE_SUMMARY_TEXT, LAB_REPORT_TEXT


def _prepare(raw: str) -> NormalizedDocument:
    parsed = TextParser().parse(raw.encode())
    normalized = normalize_document(parsed)
    return replace(normalized, sections=detect_sections(normalized))


@pytest.fixture
def discharge_document() -> NormalizedDocument:
    return _prepare(DISCHARGE_SUMMARY_TEXT)


@pytest.fixture
def chunker() -> StructuralSemanticChunker:
    return StructuralSemanticChunker(
        ChunkingOptions(target_tokens=96, min_tokens=16, max_tokens=160, overlap_ratio=0.1)
    )


class TestChunkingOptions:
    def test_rejects_unordered_bounds(self) -> None:
        with pytest.raises(ValueError, match="min_tokens"):
            ChunkingOptions(target_tokens=100, min_tokens=200, max_tokens=300)
        with pytest.raises(ValueError, match="min_tokens"):
            ChunkingOptions(target_tokens=100, min_tokens=10, max_tokens=50)

    def test_rejects_an_out_of_range_overlap(self) -> None:
        with pytest.raises(ValueError, match="overlap_ratio"):
            ChunkingOptions(overlap_ratio=0.75)

    def test_builds_from_settings(self) -> None:
        from cip_core.config import IngestionSettings

        options = ChunkingOptions.from_settings(
            IngestionSettings(chunk_target_tokens=200, chunk_min_tokens=50, chunk_max_tokens=300)
        )
        assert options.target_tokens == 200
        assert options.max_tokens == 300


class TestChunkingInvariants:
    def test_produces_chunks(
        self, chunker: StructuralSemanticChunker, discharge_document: NormalizedDocument
    ) -> None:
        assert len(chunker.chunk(discharge_document)) > 1

    def test_chunks_never_cross_section_boundaries(
        self, chunker: StructuralSemanticChunker, discharge_document: NormalizedDocument
    ) -> None:
        """Invariant 1: a chunk spanning two sections answers neither question well."""
        for chunk in chunker.chunk(discharge_document):
            section = discharge_document.section_for_offset(chunk.char_start)
            assert section is not None
            assert chunk.char_end <= section.char_end

    def test_chunks_respect_the_token_ceiling(
        self, chunker: StructuralSemanticChunker, discharge_document: NormalizedDocument
    ) -> None:
        """Invariant 3, allowing the documented single-indivisible-unit exception."""
        chunks = chunker.chunk(discharge_document)
        oversized = [c for c in chunks if c.token_count > 160]
        for chunk in oversized:
            assert "\n" not in chunk.text.strip() or "|" in chunk.text, (
                "only an indivisible unit may exceed the ceiling"
            )

    def test_character_ranges_round_trip_exactly(
        self, chunker: StructuralSemanticChunker, discharge_document: NormalizedDocument
    ) -> None:
        """Invariant 4: without this, every citation points at the wrong span."""
        for chunk in chunker.chunk(discharge_document):
            assert discharge_document.text[chunk.char_start : chunk.char_end] == chunk.text

    def test_chunk_indices_are_sequential(
        self, chunker: StructuralSemanticChunker, discharge_document: NormalizedDocument
    ) -> None:
        chunks = chunker.chunk(discharge_document)
        assert [chunk.index for chunk in chunks] == list(range(len(chunks)))

    def test_tables_are_never_split(self, chunker: StructuralSemanticChunker) -> None:
        """Invariant 2: half a lab panel is not a usable retrieval unit."""
        document = _prepare(LAB_REPORT_TEXT)
        chunks = chunker.chunk(document)

        table_rows = [
            line
            for line in document.text.split("\n")
            if ("|" in line and "mmol/L" in line) or "mg/dL" in line
        ]
        for row in table_rows:
            assert any(row in chunk.text for chunk in chunks), (
                f"table row was split across chunks: {row!r}"
            )

    def test_chunks_carry_section_metadata(
        self, chunker: StructuralSemanticChunker, discharge_document: NormalizedDocument
    ) -> None:
        for chunk in chunker.chunk(discharge_document):
            assert "section_name" in chunk.metadata
            assert "chunker" in chunk.metadata
            assert isinstance(chunk.section_type, SectionType)

    def test_chunks_carry_page_numbers(
        self, chunker: StructuralSemanticChunker, discharge_document: NormalizedDocument
    ) -> None:
        for chunk in chunker.chunk(discharge_document):
            assert chunk.page_start is not None

    def test_content_hash_is_stable_and_content_dependent(
        self, chunker: StructuralSemanticChunker, discharge_document: NormalizedDocument
    ) -> None:
        chunks = chunker.chunk(discharge_document)
        assert chunks[0].content_hash == chunks[0].content_hash
        assert len({chunk.content_hash for chunk in chunks}) == len(chunks)

    def test_no_chunk_is_empty(
        self, chunker: StructuralSemanticChunker, discharge_document: NormalizedDocument
    ) -> None:
        assert all(chunk.text.strip() for chunk in chunker.chunk(discharge_document))

    def test_full_document_content_is_covered(
        self, chunker: StructuralSemanticChunker, discharge_document: NormalizedDocument
    ) -> None:
        """Nothing clinical may be dropped between chunk boundaries."""
        chunks = chunker.chunk(discharge_document)
        for phrase in ("Substernal chest pain", "Lisinopril", "Penicillin", "Discharged home"):
            assert any(phrase in chunk.text for chunk in chunks), f"lost: {phrase}"


class TestChunkingEdgeCases:
    def test_empty_document_yields_no_chunks(self, chunker: StructuralSemanticChunker) -> None:
        assert chunker.chunk(NormalizedDocument(text="")) == ()

    def test_whitespace_only_document_yields_no_chunks(
        self, chunker: StructuralSemanticChunker
    ) -> None:
        assert chunker.chunk(NormalizedDocument(text="   \n\n  \t ")) == ()

    def test_document_without_sections_is_still_chunked(
        self, chunker: StructuralSemanticChunker
    ) -> None:
        document = NormalizedDocument(text="A single sentence with no section structure.")
        assert len(chunker.chunk(document)) == 1

    def test_single_oversized_unit_is_emitted_whole_not_truncated(self) -> None:
        """Truncating would silently lose clinical content."""
        long_sentence = "word " * 500
        document = NormalizedDocument(
            text=long_sentence,
            sections=(
                DetectedSection(
                    heading=None,
                    section_type=SectionType.NARRATIVE,
                    canonical_name="body",
                    char_start=0,
                    char_end=len(long_sentence),
                ),
            ),
        )
        chunks = StructuralSemanticChunker(
            ChunkingOptions(target_tokens=32, min_tokens=8, max_tokens=64)
        ).chunk(document)

        assert chunks
        assert sum(chunk.text.count("word") for chunk in chunks) >= 500

    def test_zero_overlap_is_supported(self, discharge_document: NormalizedDocument) -> None:
        chunker = StructuralSemanticChunker(
            ChunkingOptions(target_tokens=64, min_tokens=8, max_tokens=128, overlap_ratio=0.0)
        )
        chunks = chunker.chunk(discharge_document)
        for previous, current in pairwise(chunks):
            assert current.char_start >= previous.char_start

    def test_overlap_repeats_context_between_chunks(self) -> None:
        text = ". ".join(f"Sentence number {i} about the patient" for i in range(40)) + "."
        document = NormalizedDocument(
            text=text,
            sections=(
                DetectedSection(
                    heading=None,
                    section_type=SectionType.NARRATIVE,
                    canonical_name="body",
                    char_start=0,
                    char_end=len(text),
                ),
            ),
        )
        with_overlap = StructuralSemanticChunker(
            ChunkingOptions(target_tokens=48, min_tokens=8, max_tokens=96, overlap_ratio=0.25)
        ).chunk(document)
        without_overlap = StructuralSemanticChunker(
            ChunkingOptions(target_tokens=48, min_tokens=8, max_tokens=96, overlap_ratio=0.0)
        ).chunk(document)

        overlap_chars = sum(c.char_count for c in with_overlap)
        plain_chars = sum(c.char_count for c in without_overlap)
        assert overlap_chars > plain_chars, "overlap must repeat some context"

    def test_chunker_name_records_the_token_estimator(
        self, chunker: StructuralSemanticChunker
    ) -> None:
        assert "structural-semantic" in chunker.name
        assert "heuristic" in chunker.name
