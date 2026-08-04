"""Clinical section detection.

Clinical documents are strongly sectioned — "HISTORY OF PRESENT ILLNESS", "ALLERGIES",
"ASSESSMENT AND PLAN" — and those boundaries are the single most useful structural signal
available before embeddings exist. Chunking across a section boundary produces a chunk
whose first half is a medication list and second half is a discharge plan; retrieving it
answers neither question well.

The detector recognises headings against a canonical clinical vocabulary rather than a
generic "looks like a heading" heuristic. That choice trades recall on unusual documents
for precision on the common ones, which is the right trade here: an unrecognised heading
degrades to narrative text (harmless), while a false heading fragments a real section
(harmful, and invisible downstream).

Section *names* are canonicalised so that "HPI", "History of Present Illness", and
"HISTORY OF PRESENT ILLNESS:" all collapse to one identifier — otherwise every downstream
filter has to know all three spellings.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from cip_core.models.enums import SectionType
from cip_ingestion.domain import DetectedSection, NormalizedDocument

__all__ = [
    "CLINICAL_SECTION_PATTERNS",
    "SectionPattern",
    "detect_sections",
    "match_heading",
]


@dataclass(frozen=True, slots=True)
class SectionPattern:
    """A canonical clinical section and the surface forms that introduce it."""

    canonical_name: str
    section_type: SectionType
    aliases: tuple[str, ...]

    def matches(self, candidate: str) -> bool:
        return candidate in self.aliases


def _normalize_heading(text: str) -> str:
    """Reduce a heading to a comparable key: lowercase, punctuation-free, single-spaced."""
    cleaned = re.sub(r"[^a-z0-9 ]+", " ", text.strip().lower())
    return re.sub(r"\s+", " ", cleaned).strip()


#: Canonical clinical sections. Aliases are pre-normalised by ``_normalize_heading`` rules
#: (lowercase, no punctuation) so matching is a set lookup rather than a regex sweep.
CLINICAL_SECTION_PATTERNS: tuple[SectionPattern, ...] = (
    SectionPattern(
        "chief_complaint",
        SectionType.NARRATIVE,
        ("chief complaint", "cc", "presenting complaint", "reason for visit"),
    ),
    SectionPattern(
        "history_of_present_illness",
        SectionType.NARRATIVE,
        ("history of present illness", "hpi", "present illness", "history of presenting illness"),
    ),
    SectionPattern(
        "past_medical_history",
        SectionType.PROBLEM_LIST,
        ("past medical history", "pmh", "medical history", "prior medical history"),
    ),
    SectionPattern(
        "past_surgical_history",
        SectionType.PROBLEM_LIST,
        ("past surgical history", "psh", "surgical history"),
    ),
    SectionPattern(
        "problem_list", SectionType.PROBLEM_LIST, ("problem list", "active problems", "problems")
    ),
    SectionPattern(
        "diagnosis",
        SectionType.PROBLEM_LIST,
        (
            "diagnosis",
            "diagnoses",
            "discharge diagnosis",
            "discharge diagnoses",
            "admission diagnosis",
            "principal diagnosis",
            "final diagnosis",
        ),
    ),
    SectionPattern(
        "allergies",
        SectionType.PROBLEM_LIST,
        ("allergies", "allergy", "allergies and adverse reactions", "drug allergies", "nka"),
    ),
    SectionPattern(
        "medications",
        SectionType.NARRATIVE,
        (
            "medications",
            "medication list",
            "current medications",
            "home medications",
            "discharge medications",
            "medications on admission",
            "meds",
        ),
    ),
    SectionPattern("family_history", SectionType.NARRATIVE, ("family history", "fh", "family hx")),
    SectionPattern(
        "social_history",
        SectionType.NARRATIVE,
        ("social history", "sh", "social hx", "substance use history"),
    ),
    SectionPattern(
        "review_of_systems", SectionType.NARRATIVE, ("review of systems", "ros", "systems review")
    ),
    SectionPattern(
        "physical_examination",
        SectionType.NARRATIVE,
        ("physical examination", "physical exam", "examination", "pe", "exam"),
    ),
    SectionPattern("vital_signs", SectionType.NARRATIVE, ("vital signs", "vitals", "vs")),
    SectionPattern(
        "laboratory_results",
        SectionType.NARRATIVE,
        (
            "laboratory results",
            "laboratory data",
            "labs",
            "lab results",
            "laboratory",
            "results",
            "pertinent labs",
        ),
    ),
    SectionPattern(
        "imaging",
        SectionType.NARRATIVE,
        ("imaging", "radiology", "imaging studies", "radiologic studies", "diagnostic imaging"),
    ),
    # Radiology and other diagnostic reports use their own heading vocabulary. Without
    # these the findings body — the clinically load-bearing part of the report — falls
    # through to the document preamble, which silently breaks section filters, the
    # reranker's section affinity, and citation headings alike. Found by the Phase 2
    # end-to-end evaluation run, not by any unit test.
    SectionPattern(
        "findings",
        SectionType.NARRATIVE,
        ("findings", "imaging findings", "examination findings", "gross findings"),
    ),
    SectionPattern(
        "technique",
        SectionType.NARRATIVE,
        ("technique", "exam technique", "examination technique"),
    ),
    SectionPattern(
        "comparison", SectionType.NARRATIVE, ("comparison", "comparison studies", "prior studies")
    ),
    SectionPattern(
        "indication",
        SectionType.NARRATIVE,
        ("indication", "indications", "clinical indication", "reason for exam"),
    ),
    SectionPattern(
        "procedures", SectionType.NARRATIVE, ("procedures", "procedure", "operations performed")
    ),
    SectionPattern(
        "hospital_course",
        SectionType.NARRATIVE,
        ("hospital course", "course in hospital", "clinical course", "brief hospital course"),
    ),
    SectionPattern(
        "assessment",
        SectionType.NARRATIVE,
        ("assessment", "impression", "clinical impression", "assessment and plan", "a p"),
    ),
    SectionPattern(
        "plan", SectionType.NARRATIVE, ("plan", "treatment plan", "discharge plan", "management")
    ),
    SectionPattern("disposition", SectionType.NARRATIVE, ("disposition", "discharge disposition")),
    SectionPattern(
        "follow_up",
        SectionType.NARRATIVE,
        ("follow up", "followup", "follow up instructions", "discharge instructions"),
    ),
    SectionPattern(
        "attestation",
        SectionType.OTHER,
        ("attestation", "electronically signed by", "signature", "signed by", "dictated by"),
    ),
)

_ALIAS_INDEX: dict[str, SectionPattern] = {
    alias: pattern for pattern in CLINICAL_SECTION_PATTERNS for alias in pattern.aliases
}

#: A heading line is short and standalone. The upper bound rejects a sentence that merely
#: starts with a section word ("Assessment of the patient's condition showed ...").
_MAX_HEADING_CHARS = 80


def match_heading(line: str) -> SectionPattern | None:
    """Return the section a line introduces, or ``None`` if it is not a heading.

    Handles the two forms clinical documents use: a heading on its own line, and an
    inline ``HEADING: content`` prefix.
    """
    stripped = line.strip()
    if not stripped or len(stripped) > _MAX_HEADING_CHARS:
        return None

    direct = _ALIAS_INDEX.get(_normalize_heading(stripped))
    if direct is not None:
        return direct

    # `HEADING: value on the same line` — split once and test only the label.
    if ":" in stripped:
        label = stripped.split(":", 1)[0]
        if len(label) <= _MAX_HEADING_CHARS:
            return _ALIAS_INDEX.get(_normalize_heading(label))
    return None


def detect_sections(document: NormalizedDocument) -> tuple[DetectedSection, ...]:
    """Locate clinical sections within normalised text.

    Returns contiguous, non-overlapping spans covering the whole document. Text preceding
    the first recognised heading becomes an untitled ``document_preamble`` section rather
    than being dropped, because the opening lines of a clinical note routinely carry the
    encounter context that later sections refer back to.
    """
    text = document.text
    if not text:
        return ()

    boundaries: list[tuple[int, int, str | None, SectionPattern | None]] = []
    offset = 0
    for line in text.split("\n"):
        line_length = len(line)
        pattern = match_heading(line)
        if pattern is not None:
            boundaries.append((offset, offset + line_length, line.strip(), pattern))
        offset += line_length + 1  # +1 for the newline consumed by split

    if not boundaries:
        return (
            DetectedSection(
                heading=None,
                section_type=SectionType.NARRATIVE,
                canonical_name="document_body",
                char_start=0,
                char_end=len(text),
                page_start=document.page_for_offset(0),
                page_end=document.page_for_offset(max(0, len(text) - 1)),
                confidence=0.5,
            ),
        )

    sections: list[DetectedSection] = []

    first_start = boundaries[0][0]
    if first_start > 0:
        sections.append(
            DetectedSection(
                heading=None,
                section_type=SectionType.OTHER,
                canonical_name="document_preamble",
                char_start=0,
                char_end=first_start,
                page_start=document.page_for_offset(0),
                page_end=document.page_for_offset(max(0, first_start - 1)),
                confidence=0.5,
            )
        )

    for index, (start, _heading_end, heading, pattern) in enumerate(boundaries):
        end = boundaries[index + 1][0] if index + 1 < len(boundaries) else len(text)
        if end <= start or pattern is None:
            continue
        sections.append(
            DetectedSection(
                heading=heading,
                section_type=pattern.section_type,
                canonical_name=pattern.canonical_name,
                char_start=start,
                char_end=end,
                page_start=document.page_for_offset(start),
                page_end=document.page_for_offset(max(start, end - 1)),
                confidence=0.9,
            )
        )

    return tuple(sections)
