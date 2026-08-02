"""Document processing stages: normalisation, section detection, metadata, chunking, quality.

Every function and class here is pure with respect to I/O — no database, no storage, no
network. That is what lets the bulk of the pipeline be tested without any backing service
and keeps the stages reusable from both the API and the CLI.
"""

from cip_ingestion.processing.chunking import (
    Chunker,
    ChunkingOptions,
    StructuralSemanticChunker,
)
from cip_ingestion.processing.metadata import (
    classify_document_type,
    detect_language,
    detect_phi_indicators,
    extract_effective_date,
    extract_metadata,
)
from cip_ingestion.processing.normalization import (
    NormalizationOptions,
    normalize_document,
    normalize_text,
)
from cip_ingestion.processing.quality import QualityCheck, QualityReport, assess_quality
from cip_ingestion.processing.sections import detect_sections, match_heading
from cip_ingestion.processing.tokenization import (
    HeuristicTokenEstimator,
    TokenEstimator,
    split_sentences,
)

__all__ = [
    "Chunker",
    "ChunkingOptions",
    "HeuristicTokenEstimator",
    "NormalizationOptions",
    "QualityCheck",
    "QualityReport",
    "StructuralSemanticChunker",
    "TokenEstimator",
    "assess_quality",
    "classify_document_type",
    "detect_language",
    "detect_phi_indicators",
    "detect_sections",
    "extract_effective_date",
    "extract_metadata",
    "match_heading",
    "normalize_document",
    "normalize_text",
    "split_sentences",
]
