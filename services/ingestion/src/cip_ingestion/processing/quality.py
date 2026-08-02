"""Data-quality assessment.

A document can pass every technical step — downloaded, parsed, chunked, persisted — and
still be useless for retrieval: OCR produced garbage, extraction recovered three
characters per page, the text is mojibake. Without a gate, those documents enter the
corpus indistinguishable from good ones and quietly degrade retrieval quality, which is
the staleness/decay failure mode called out in
docs/architecture/02-rag-hybrid-retrieval.md §4.

The gate produces a *score and a verdict*, not a boolean. Three outcomes are needed
because the middle one is real: ``PASS`` (use it), ``WARN`` (usable, degradation
recorded), ``FAIL`` (quarantine for review). Quarantine specifically is not deletion — the
bytes are stored and the document is reprocessable once OCR improves or a parser is fixed.

Every check's inputs and result are persisted (``document_quality_reports.checks``), so a
future threshold change can be evaluated against historical documents without re-running
the pipeline.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from typing import Any

from cip_core.models.enums import QualityVerdict
from cip_ingestion.domain import DocumentMetadata, NormalizedDocument, ParsedDocument, TextChunk

__all__ = ["QualityCheck", "QualityReport", "assess_quality", "count_garbled_characters"]

#: Unicode general categories that indicate a decoding or OCR failure rather than
#: clinical content: Cc (control), Cf (format), Cs (surrogate), Co (private use),
#: Cn (unassigned). Expressed as categories instead of a character-class regex because
#: the latter requires literal control characters in source, which are fragile to move
#: between tools and unreadable in review.
_GARBLED_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn"})

#: U+FFFD, emitted by every lossy decode, is the strongest single mojibake signal.
_REPLACEMENT_CHARACTER = chr(0xFFFD)

#: A page yielding fewer characters than this recovered essentially nothing.
_MIN_CHARS_PER_PAGE = 24

#: OCR mean confidence below this is unreliable enough to warrant review.
_MIN_OCR_CONFIDENCE = 0.55

#: Above this ratio of garbled characters the text is not clinically readable.
_MAX_GARBLED_RATIO = 0.02


def count_garbled_characters(text: str) -> int:
    """Count characters that indicate a decoding or OCR failure.

    Whitespace is excluded explicitly: tabs and newlines are category Cc but are
    legitimate structure, and counting them would mark every document as garbled.
    """
    return sum(
        1
        for char in text
        if char == _REPLACEMENT_CHARACTER
        or (not char.isspace() and unicodedata.category(char) in _GARBLED_CATEGORIES)
    )


@dataclass(frozen=True, slots=True)
class QualityCheck:
    """One quality dimension's result."""

    name: str
    passed: bool
    score: float
    """Normalised [0, 1] quality for this dimension."""
    weight: float
    detail: str
    observed: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "score": round(self.score, 4),
            "weight": self.weight,
            "detail": self.detail,
            "observed": self.observed,
        }


@dataclass(frozen=True, slots=True)
class QualityReport:
    """Aggregate quality assessment for one ingestion run."""

    verdict: QualityVerdict
    score: float
    checks: tuple[QualityCheck, ...]

    @property
    def failed_checks(self) -> tuple[QualityCheck, ...]:
        return tuple(check for check in self.checks if not check.passed)

    def to_json(self) -> dict[str, Any]:
        return {
            "verdict": str(self.verdict),
            "score": round(self.score, 4),
            "checks": [check.to_json() for check in self.checks],
            "failed_checks": [check.name for check in self.failed_checks],
        }


def _extraction_yield_check(parsed: ParsedDocument, normalized: NormalizedDocument) -> QualityCheck:
    """Did parsing recover a plausible amount of text per page?"""
    pages = max(parsed.page_count, 1)
    chars_per_page = normalized.char_count / pages
    # Saturates at 1.0 by ~10x the minimum, so a dense report and a very dense report
    # score the same rather than letting length dominate the aggregate.
    score = min(1.0, chars_per_page / (_MIN_CHARS_PER_PAGE * 10))
    passed = chars_per_page >= _MIN_CHARS_PER_PAGE
    return QualityCheck(
        name="extraction_yield",
        passed=passed,
        score=score,
        weight=3.0,
        detail=(
            f"{chars_per_page:.1f} characters/page recovered"
            if passed
            else f"only {chars_per_page:.1f} characters/page recovered"
        ),
        observed={"chars_per_page": round(chars_per_page, 2), "page_count": pages},
    )


def _empty_page_check(parsed: ParsedDocument) -> QualityCheck:
    """What fraction of pages yielded no text at all?"""
    pages = max(parsed.page_count, 1)
    empty = sum(1 for page in parsed.pages if page.char_count == 0)
    ratio = empty / pages
    return QualityCheck(
        name="empty_pages",
        passed=ratio <= 0.34,
        score=max(0.0, 1.0 - ratio),
        weight=2.0,
        detail=f"{empty} of {pages} page(s) yielded no text",
        observed={"empty_pages": empty, "page_count": pages, "ratio": round(ratio, 4)},
    )


def _ocr_confidence_check(parsed: ParsedDocument) -> QualityCheck | None:
    """Was OCR confident? Returns ``None`` when no page needed OCR."""
    if parsed.ocr_page_count == 0:
        return None
    confidence = parsed.mean_ocr_confidence
    if confidence is None:
        return QualityCheck(
            name="ocr_confidence",
            passed=True,
            score=0.6,
            weight=1.0,
            detail="OCR applied but the engine reported no confidence scores",
            observed={"ocr_pages": parsed.ocr_page_count},
        )
    return QualityCheck(
        name="ocr_confidence",
        passed=confidence >= _MIN_OCR_CONFIDENCE,
        score=min(1.0, confidence),
        weight=2.5,
        detail=f"mean OCR confidence {confidence:.2f} across {parsed.ocr_page_count} page(s)",
        observed={
            "mean_confidence": round(confidence, 4),
            "ocr_pages": parsed.ocr_page_count,
        },
    )


def _garbled_text_check(normalized: NormalizedDocument) -> QualityCheck:
    """Is the text readable, or is it decoding/OCR noise?"""
    total = max(normalized.char_count, 1)
    garbled = count_garbled_characters(normalized.text)
    ratio = garbled / total
    return QualityCheck(
        name="garbled_text",
        passed=ratio <= _MAX_GARBLED_RATIO,
        score=max(0.0, 1.0 - (ratio / _MAX_GARBLED_RATIO if _MAX_GARBLED_RATIO else 1.0)),
        weight=3.0,
        detail=f"{garbled} unreadable character(s) ({ratio:.2%} of text)",
        observed={"garbled_chars": garbled, "ratio": round(ratio, 6)},
    )


def _chunking_check(chunks: tuple[TextChunk, ...], normalized: NormalizedDocument) -> QualityCheck:
    """Did chunking produce usable retrieval units covering the document?"""
    if not chunks:
        return QualityCheck(
            name="chunking",
            passed=False,
            score=0.0,
            weight=3.0,
            detail="no chunks were produced",
            observed={"chunk_count": 0},
        )
    covered = sum(chunk.char_count for chunk in chunks)
    # Overlap makes coverage exceed 1.0; clamping keeps the score meaningful rather than
    # rewarding a chunker that simply duplicates content.
    coverage = min(1.0, covered / max(normalized.char_count, 1))
    return QualityCheck(
        name="chunking",
        passed=coverage >= 0.7,
        score=coverage,
        weight=2.0,
        detail=f"{len(chunks)} chunk(s) covering {coverage:.0%} of the document",
        observed={"chunk_count": len(chunks), "coverage": round(coverage, 4)},
    )


def _parser_warning_check(parsed: ParsedDocument) -> QualityCheck:
    """How degraded was parsing?"""
    count = len(parsed.warnings)
    pages = max(parsed.page_count, 1)
    ratio = count / pages
    return QualityCheck(
        name="parser_warnings",
        passed=ratio <= 0.5,
        score=max(0.0, 1.0 - ratio),
        weight=1.0,
        detail=f"{count} parser warning(s) across {pages} page(s)",
        observed={"warning_count": count, "warnings": list(parsed.warnings[:20])},
    )


def _metadata_check(metadata: DocumentMetadata) -> QualityCheck:
    """Was enough metadata recovered for the document to be findable and filterable?

    Informational by design — this check never fails a document. A discharge summary with
    no recoverable date is still clinically valuable; the missing attribute should be
    visible, not disqualifying.
    """
    present = sum(
        1
        for value in (metadata.title, metadata.effective_date, metadata.language)
        if value is not None
    )
    if metadata.document_type_confidence >= 0.5:
        present += 1
    score = present / 4
    return QualityCheck(
        name="metadata_completeness",
        passed=True,
        score=score,
        weight=1.0,
        detail=f"{present} of 4 key metadata attributes recovered",
        observed={
            "has_title": metadata.title is not None,
            "has_effective_date": metadata.effective_date is not None,
            "has_language": metadata.language is not None,
            "document_type": str(metadata.document_type),
            "document_type_confidence": metadata.document_type_confidence,
        },
    )


def assess_quality(
    *,
    parsed: ParsedDocument,
    normalized: NormalizedDocument,
    chunks: tuple[TextChunk, ...],
    metadata: DocumentMetadata,
    min_score: float = 0.60,
) -> QualityReport:
    """Run all quality checks and return the aggregate report.

    A critical check failing (extraction yield, garbled text, chunking) forces ``FAIL``
    regardless of the weighted score — otherwise a document with unreadable text but tidy
    metadata could average its way past the threshold, which is precisely the document the
    gate exists to catch.
    """
    checks: list[QualityCheck] = [
        _extraction_yield_check(parsed, normalized),
        _empty_page_check(parsed),
        _garbled_text_check(normalized),
        _chunking_check(chunks, normalized),
        _parser_warning_check(parsed),
        _metadata_check(metadata),
    ]
    ocr_check = _ocr_confidence_check(parsed)
    if ocr_check is not None:
        checks.append(ocr_check)

    total_weight = sum(check.weight for check in checks) or 1.0
    score = sum(check.score * check.weight for check in checks) / total_weight

    critical = {"extraction_yield", "garbled_text", "chunking"}
    critical_failed = any(check.name in critical and not check.passed for check in checks)

    if critical_failed or score < min_score:
        verdict = QualityVerdict.FAIL
    elif any(not check.passed for check in checks):
        verdict = QualityVerdict.WARN
    else:
        verdict = QualityVerdict.PASS

    return QualityReport(verdict=verdict, score=round(score, 4), checks=tuple(checks))
