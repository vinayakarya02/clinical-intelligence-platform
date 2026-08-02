"""Document metadata extraction and classification.

Produces the metadata recorded on the document row: title, author, effective date,
language, document type, and PHI indicators.

Two aspects deserve explanation.

**Document-type classification is scored, not decided.** The classifier returns a
confidence alongside the type, and the pipeline records ``UNKNOWN`` rather than committing
to a low-confidence guess. A misclassified document is worse than an unclassified one
because downstream filters trust the label.

**PHI detection reports categories, never values.** The extractor records that an MRN-like
pattern was found, not the MRN itself. Writing the matched value into a metadata column
would relocate PHI into a field that is not treated as PHI — the exact failure the
redaction processor in :mod:`cip_core.logging` exists to prevent.
"""

from __future__ import annotations

import datetime as dt
import re
from collections import defaultdict

from cip_core.models.enums import DocumentType
from cip_ingestion.domain import DetectedSection, DocumentMetadata, NormalizedDocument

__all__ = [
    "classify_document_type",
    "detect_language",
    "detect_phi_indicators",
    "extract_effective_date",
    "extract_metadata",
]

#: Section/keyword evidence per document type. Weights reflect discriminative power:
#: "hospital course" almost only appears in discharge summaries, while "impression"
#: appears in radiology notes and discharge summaries alike.
_TYPE_EVIDENCE: dict[DocumentType, tuple[tuple[str, float], ...]] = {
    DocumentType.DISCHARGE_SUMMARY: (
        ("hospital course", 3.0),
        ("discharge diagnosis", 3.0),
        ("discharge medications", 2.5),
        ("discharge instructions", 2.0),
        ("disposition", 1.5),
        ("admission date", 1.5),
        ("discharge summary", 4.0),
    ),
    DocumentType.LAB_REPORT: (
        ("reference range", 3.0),
        ("specimen", 2.5),
        ("collected", 1.5),
        ("laboratory report", 4.0),
        ("mmol/l", 2.0),
        ("mg/dl", 2.0),
        ("white blood cell", 1.5),
        ("hemoglobin", 1.5),
    ),
    DocumentType.RADIOLOGY_NOTE: (
        ("findings", 2.0),
        ("impression", 1.5),
        ("technique", 2.0),
        ("contrast", 1.5),
        ("radiology report", 4.0),
        ("comparison", 1.5),
        ("ct scan", 2.0),
        ("mri", 2.0),
        ("radiograph", 2.0),
    ),
    DocumentType.TRIAL_PROTOCOL: (
        ("inclusion criteria", 3.5),
        ("exclusion criteria", 3.5),
        ("primary endpoint", 3.0),
        ("secondary endpoint", 2.5),
        ("study protocol", 4.0),
        ("informed consent", 2.0),
        ("randomiz", 2.0),
    ),
    DocumentType.ADVERSE_EVENT_REPORT: (
        ("adverse event", 4.0),
        ("serious adverse", 3.5),
        ("suspected reaction", 3.0),
        ("causality", 2.5),
        ("meddra", 3.0),
        ("pharmacovigilance", 3.0),
    ),
    DocumentType.GUIDELINE: (
        ("recommendation", 2.5),
        ("class of recommendation", 3.5),
        ("level of evidence", 3.5),
        ("clinical practice guideline", 4.0),
        ("grade of evidence", 3.0),
    ),
    DocumentType.LITERATURE: (
        ("abstract", 2.5),
        ("methods", 1.5),
        ("references", 2.0),
        ("doi", 2.5),
        ("pubmed", 2.5),
        ("we conducted", 2.0),
    ),
}

#: Minimum score before a classification is trusted. Below it the type is UNKNOWN.
_CLASSIFICATION_MIN_SCORE = 4.0

#: PHI patterns. Category names only ever leave this module — never the matched text.
_PHI_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("mrn", re.compile(r"\b(?:mrn|medical record (?:number|no\.?))\b[:\s#]*[A-Z0-9-]{4,}", re.I)),
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("phone", re.compile(r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b")),
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]{2,}\b")),
    ("date_of_birth", re.compile(r"\b(?:dob|date of birth)\b[:\s]*\d", re.I)),
    ("patient_name", re.compile(r"\b(?:patient name|name)\s*:\s*[A-Z][a-z]+", re.I)),
    ("address", re.compile(r"\b\d{1,5}\s+[A-Z][a-z]+\s+(?:st|street|ave|avenue|rd|road)\b", re.I)),
)

#: High-frequency English function words. Presence/absence separates English from other
#: Latin-script languages well enough to tag a corpus, without a model dependency that
#: would need its own licensing and update story.
_ENGLISH_MARKERS = frozenset(
    {"the", "and", "of", "to", "in", "with", "was", "for", "on", "is", "no", "patient"}
)

_DATE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"), "ymd"),
    (re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b"), "mdy"),
    (
        re.compile(
            r"\b(January|February|March|April|May|June|July|August|September|October|"
            r"November|December)\s+(\d{1,2}),?\s+(\d{4})\b",
            re.I,
        ),
        "mname",
    ),
)

_MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}

#: Dates outside this window are almost certainly parse artefacts (a lab value read as a
#: year, a template placeholder) rather than a real effective date.
_MIN_PLAUSIBLE_YEAR = 1900
_MAX_PLAUSIBLE_YEAR = 2100

_DATE_CONTEXT = re.compile(
    r"(?:date of service|service date|date|admitted|admission date|discharge date|"
    r"collected|reported|examination date|study date|visit date)\b[:\s]*$",
    re.I,
)


def detect_language(text: str) -> str | None:
    """Return an ISO 639-1 code, or ``None`` when undetermined.

    Only English is positively identified in Phase 1. Returning ``None`` rather than
    defaulting to ``en`` keeps "we did not detect a language" distinguishable from "this
    is English", which matters once non-English corpora are onboarded.
    """
    words = re.findall(r"[a-z']+", text.lower())
    if len(words) < 20:
        return None
    marker_hits = sum(1 for word in words if word in _ENGLISH_MARKERS)
    return "en" if marker_hits / len(words) >= 0.06 else None


def classify_document_type(
    text: str, section_names: tuple[str, ...] = ()
) -> tuple[DocumentType, float]:
    """Classify a document by weighted keyword evidence.

    Returns ``(type, confidence)`` where confidence is in [0, 1]. Confidence is derived
    from the winning score's margin over the runner-up, not its absolute value: a document
    matching many types strongly is genuinely ambiguous and should not be reported as
    confidently classified.
    """
    haystack = text.lower()
    section_blob = " ".join(section_names).lower()

    # A plain dict rather than Counter: evidence weights are floats, and Counter is
    # typed for integer counts.
    scores: dict[DocumentType, float] = defaultdict(float)
    for doc_type, evidence in _TYPE_EVIDENCE.items():
        for term, weight in evidence:
            if term in haystack:
                scores[doc_type] += weight
            if term in section_blob:
                scores[doc_type] += weight * 0.5

    if not scores:
        return DocumentType.UNKNOWN, 0.0

    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)[:2]
    best_type, best_score = ranked[0]
    if best_score < _CLASSIFICATION_MIN_SCORE:
        return DocumentType.UNKNOWN, round(min(best_score / _CLASSIFICATION_MIN_SCORE, 1.0), 4)

    runner_up = ranked[1][1] if len(ranked) > 1 else 0.0
    margin = (best_score - runner_up) / best_score
    confidence = min(1.0, 0.5 + 0.5 * margin)
    return best_type, round(confidence, 4)


def detect_phi_indicators(text: str) -> tuple[str, ...]:
    """Return the categories of potential PHI present. Never returns matched values."""
    return tuple(sorted(category for category, pattern in _PHI_PATTERNS if pattern.search(text)))


def _coerce_date(match: re.Match[str], kind: str) -> dt.date | None:
    try:
        if kind == "ymd":
            year, month, day = int(match[1]), int(match[2]), int(match[3])
        elif kind == "mdy":
            month, day, year = int(match[1]), int(match[2]), int(match[3])
        else:
            month = _MONTHS[match[1].lower()]
            day, year = int(match[2]), int(match[3])
        if not _MIN_PLAUSIBLE_YEAR <= year <= _MAX_PLAUSIBLE_YEAR:
            return None
        return dt.date(year, month, day)
    except (ValueError, KeyError):
        return None


def extract_effective_date(text: str) -> dt.date | None:
    """Find the document's clinically effective date.

    Prefers a date preceded by an explicit label ("Date of Service: ...") over the first
    date in the document, because clinical documents routinely open with a printed-on
    timestamp or a patient date of birth — neither of which is the effective date, and the
    latter of which is PHI that must not become a searchable document attribute.
    """
    labelled: dt.date | None = None
    fallback: dt.date | None = None

    for pattern, kind in _DATE_PATTERNS:
        for match in pattern.finditer(text):
            parsed = _coerce_date(match, kind)
            if parsed is None:
                continue
            preceding = text[max(0, match.start() - 40) : match.start()]
            if _DATE_CONTEXT.search(preceding.rstrip()):
                if labelled is None or parsed > labelled:
                    labelled = parsed
            elif fallback is None:
                fallback = parsed

    return labelled or fallback


def extract_metadata(
    document: NormalizedDocument,
    sections: tuple[DetectedSection, ...],
    *,
    parser_properties: dict[str, object] | None = None,
    page_count: int = 0,
    filename: str | None = None,
) -> DocumentMetadata:
    """Build the metadata record for a normalised document."""
    properties = parser_properties or {}
    text = document.text

    section_names = tuple(dict.fromkeys(section.canonical_name for section in sections))
    document_type, confidence = classify_document_type(text, section_names)

    title = _select_title(properties, sections, text, filename)
    author = properties.get("author")

    return DocumentMetadata(
        title=title,
        author=str(author)[:256] if isinstance(author, str) and author.strip() else None,
        document_type=document_type,
        document_type_confidence=confidence,
        effective_date=extract_effective_date(text),
        language=detect_language(text),
        page_count=page_count,
        word_count=len(re.findall(r"\S+", text)),
        section_names=section_names,
        phi_indicators=detect_phi_indicators(text),
        extra={
            "removed_line_count": document.removed_line_count,
            "normalized_char_count": document.char_count,
            "source_char_count": document.source_char_count,
        },
    )


def _select_title(
    properties: dict[str, object],
    sections: tuple[DetectedSection, ...],
    text: str,
    filename: str | None,
) -> str | None:
    """Choose a title from the best available source.

    Preference order: format metadata (authored deliberately), then the document's own
    opening line, then a section heading, then the filename.

    The opening line outranks section headings because a section heading is never a
    document title — "CHIEF COMPLAINT" describes a part of the document, not the document.
    A line that is itself a recognised section heading is therefore skipped, which is what
    keeps a note that opens directly with "HISTORY OF PRESENT ILLNESS" from adopting it as
    a title. Producer-generated titles that are really filenames ("Microsoft Word -
    report.doc") are rejected for the same reason: they identify a file, not its content.
    """
    raw_title = properties.get("title")
    if isinstance(raw_title, str):
        candidate = raw_title.strip()
        if candidate and not candidate.lower().startswith(("microsoft word", "untitled")):
            return candidate[:512]

    from cip_ingestion.processing.sections import match_heading

    for line in text.split("\n"):
        stripped = line.strip()
        if 8 <= len(stripped) <= 160 and match_heading(stripped) is None:
            return stripped[:512]

    for section in sections:
        if section.heading:
            return section.heading[:512]

    return filename[:512] if filename else None
