"""Pure document processing.

Everything between "here are some bytes" and "here are chunks and a quality verdict",
with no storage, database, or network access. Splitting this out from
:mod:`cip_ingestion.pipeline` is what makes the core of the pipeline testable without any
backing service — the tests that matter most (does chunking respect section boundaries?
does a scanned document get quarantined?) run in milliseconds against real inputs.

Stage timings are collected here rather than in the orchestrator because this is where
the time actually goes: OCR and parsing dominate, and attributing a slow ingest to a
stage is the first question asked when one does.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace

from cip_core.config import IngestionSettings
from cip_core.logging import get_logger
from cip_core.models.enums import DocumentType, PipelineStage
from cip_ingestion.domain import (
    DocumentMetadata,
    NormalizedDocument,
    ParsedDocument,
    TextChunk,
)
from cip_ingestion.parsers.base import ParserRegistry
from cip_ingestion.processing.chunking import Chunker, ChunkingOptions, StructuralSemanticChunker
from cip_ingestion.processing.metadata import extract_metadata
from cip_ingestion.processing.normalization import NormalizationOptions, normalize_document
from cip_ingestion.processing.quality import QualityReport, assess_quality
from cip_ingestion.processing.sections import detect_sections

__all__ = ["DocumentProcessor", "ProcessingResult", "StageTimer"]

_log = get_logger(__name__)


class StageTimer:
    """Accumulates per-stage wall-clock durations in milliseconds."""

    def __init__(self) -> None:
        self._durations: dict[str, float] = {}

    def record(self, stage: PipelineStage, duration_ms: float) -> None:
        # Accumulate rather than overwrite: a stage can legitimately run more than once
        # in a pipeline that retries, and the total is the useful number.
        self._durations[str(stage)] = self._durations.get(str(stage), 0.0) + duration_ms

    def time(self, stage: PipelineStage) -> _StageContext:
        return _StageContext(self, stage)

    @property
    def durations(self) -> dict[str, float]:
        return {stage: round(value, 3) for stage, value in self._durations.items()}

    @property
    def total_ms(self) -> float:
        return round(sum(self._durations.values()), 3)


class _StageContext:
    """Context manager recording one stage's duration, including on failure."""

    __slots__ = ("_stage", "_start", "_timer")

    def __init__(self, timer: StageTimer, stage: PipelineStage) -> None:
        self._timer = timer
        self._stage = stage
        self._start = 0.0

    def __enter__(self) -> _StageContext:
        self._start = time.perf_counter()
        return self

    def __exit__(self, *exc_info: object) -> None:
        # Records unconditionally so a stage that raised still shows how long it ran
        # before failing — usually the most interesting timing of all.
        self._timer.record(self._stage, (time.perf_counter() - self._start) * 1000)


@dataclass(frozen=True, slots=True)
class ProcessingResult:
    """Output of the pure processing stages."""

    parsed: ParsedDocument
    normalized: NormalizedDocument
    metadata: DocumentMetadata
    chunks: tuple[TextChunk, ...]
    quality: QualityReport
    stage_durations_ms: dict[str, float] = field(default_factory=dict)

    @property
    def parser_name(self) -> str:
        return self.parsed.parser_name


class DocumentProcessor:
    """Runs parse → normalise → sections → metadata → chunk → quality."""

    def __init__(
        self,
        *,
        parsers: ParserRegistry,
        settings: IngestionSettings,
        chunker: Chunker | None = None,
        normalization_options: NormalizationOptions | None = None,
    ) -> None:
        self._parsers = parsers
        self._settings = settings
        self._chunker = chunker or StructuralSemanticChunker(
            ChunkingOptions.from_settings(settings)
        )
        self._normalization_options = normalization_options or NormalizationOptions()

    @property
    def chunker_name(self) -> str:
        return self._chunker.name

    def process(
        self,
        data: bytes,
        *,
        media_type: str,
        filename: str | None = None,
        declared_document_type: DocumentType | None = None,
        timer: StageTimer | None = None,
    ) -> ProcessingResult:
        """Process raw bytes into chunks and a quality verdict.

        ``declared_document_type`` overrides classification when the caller knows the type
        — an integration pulling from a lab system's "results" endpoint has better
        information than any classifier, and should not have its knowledge second-guessed.
        """
        timer = timer or StageTimer()

        with timer.time(PipelineStage.PARSE):
            parser = self._parsers.get(media_type)
            parsed = parser.parse(data, filename=filename)

        with timer.time(PipelineStage.NORMALIZE):
            normalized = normalize_document(parsed, self._normalization_options)

        with timer.time(PipelineStage.DETECT_SECTIONS):
            sections = detect_sections(normalized)
            normalized = replace(normalized, sections=sections)

        with timer.time(PipelineStage.EXTRACT_METADATA):
            metadata = extract_metadata(
                normalized,
                sections,
                parser_properties=parsed.properties,
                page_count=parsed.page_count,
                filename=filename,
            )
            if declared_document_type is not None:
                metadata = replace(
                    metadata,
                    document_type=declared_document_type,
                    document_type_confidence=1.0,
                )

        with timer.time(PipelineStage.CHUNK):
            chunks = self._chunker.chunk(normalized) if metadata.document_type.is_narrative else ()
            if not metadata.document_type.is_narrative:
                # Structured interchange formats populate the relational and graph stores
                # directly and are never chunk-embedded
                # (docs/architecture/02-rag-hybrid-retrieval.md §1.1).
                _log.info(
                    "chunking.skipped",
                    reason="structured_document_type",
                    document_type=str(metadata.document_type),
                )

        with timer.time(PipelineStage.QUALITY_CHECK):
            quality = assess_quality(
                parsed=parsed,
                normalized=normalized,
                chunks=chunks,
                metadata=metadata,
                min_score=self._settings.quality_min_score,
            )

        _log.info(
            "document.processed",
            parser=parsed.parser_name,
            page_count=parsed.page_count,
            chunk_count=len(chunks),
            quality_verdict=str(quality.verdict),
            quality_score=quality.score,
            duration_ms=timer.total_ms,
        )

        return ProcessingResult(
            parsed=parsed,
            normalized=normalized,
            metadata=metadata,
            chunks=chunks,
            quality=quality,
            stage_durations_ms=timer.durations,
        )
