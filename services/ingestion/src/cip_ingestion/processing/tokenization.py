"""Token estimation and clinical sentence segmentation.

**Token estimation.** Chunk budgets are expressed in tokens, but the authoritative
tokenizer belongs to the embedding model — and that model is not selected until the
Phase 1 bake-off concludes (docs/architecture/02-rag-hybrid-retrieval.md §1.3). Committing
to one tokenizer now would either force an arbitrary model choice or bind chunking to a
model the platform may not ship. So token counting sits behind :class:`TokenEstimator`,
with a calibrated heuristic as the Phase 1 implementation and a real tokenizer dropped in
during Phase 2 without touching the chunker.

**Sentence segmentation.** Splitting clinical text on ``[.!?]`` is wrong in ways that
matter: "Pt. given 2.5 mg q.i.d. per Dr. Smith" contains five periods and one sentence.
Breaking there produces chunks that split a dose from its drug. The segmenter therefore
protects known clinical abbreviations, decimals, and enumerations before splitting.
"""

from __future__ import annotations

import re
from typing import Protocol, runtime_checkable

__all__ = [
    "HeuristicTokenEstimator",
    "TokenEstimator",
    "split_sentences",
]

#: Abbreviations whose trailing period does not end a sentence. Chosen from what actually
#: appears in clinical documentation: honorifics, dosing shorthand, units, and the Latin
#: abbreviations that survive in prescriptions.
_PROTECTED_ABBREVIATIONS = (
    "dr",
    "drs",
    "mr",
    "mrs",
    "ms",
    "prof",
    "st",
    "jr",
    "sr",
    "pt",
    "pts",
    "hx",
    "dx",
    "tx",
    "rx",
    "sx",
    "fx",
    "approx",
    "vs",
    "etc",
    "e.g",
    "i.e",
    "cf",
    "al",
    "q.d",
    "b.i.d",
    "t.i.d",
    "q.i.d",
    "p.r.n",
    "p.o",
    "i.v",
    "i.m",
    "s.c",
    "q.h.s",
    "a.c",
    "p.c",
    "n.p.o",
    "s.l",
    "mg",
    "mcg",
    "kg",
    "ml",
    "dl",
    "mmol",
    "meq",
    "cm",
    "mm",
    "no",
    "inc",
    "ltd",
    "dept",
    "univ",
    "assoc",
)

_ABBREV_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(abbr) for abbr in _PROTECTED_ABBREVIATIONS) + r")\.",
    re.IGNORECASE,
)

#: Sentinel substituted for a protected period. Uses a private-use codepoint so it cannot
#: collide with document content.
_PERIOD_SENTINEL = ""

_DECIMAL_PATTERN = re.compile(r"(\d)\.(\d)")
_INITIAL_PATTERN = re.compile(r"\b([A-Z])\.(?=\s*[A-Z])")
_ENUMERATION_PATTERN = re.compile(r"(?:^|\n)\s*(\d{1,2})\.(?=\s)")

_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])[ \t]+(?=[\"'(\[]?[A-Z0-9])")


@runtime_checkable
class TokenEstimator(Protocol):
    """Counts tokens in a string."""

    @property
    def name(self) -> str: ...

    def count(self, text: str) -> int: ...


class HeuristicTokenEstimator:
    """Model-agnostic token estimate.

    Subword tokenizers emit roughly one token per short word, more for long or rare words,
    and one per punctuation mark. The estimate below models that directly — words
    contribute ``ceil(len / chars_per_token)`` tokens, punctuation one each — which tracks
    real tokenizer output far better than a flat ``len(text) / 4`` and, critically,
    *over*-estimates dense clinical text rather than under-estimating it. Overshooting
    produces chunks slightly under the model's true limit; undershooting produces chunks
    that get truncated at embedding time, silently losing their tail.
    """

    def __init__(self, *, chars_per_token: float = 4.0) -> None:
        if chars_per_token <= 0:
            raise ValueError("chars_per_token must be positive")
        self._chars_per_token = chars_per_token

    @property
    def name(self) -> str:
        return f"heuristic-{self._chars_per_token:g}"

    def count(self, text: str) -> int:
        if not text.strip():
            return 0
        tokens = 0
        for word in re.findall(r"[^\W_]+|[^\w\s]", text, flags=re.UNICODE):
            if word.isalnum():
                tokens += max(1, -(-len(word) // int(self._chars_per_token)))
            else:
                tokens += 1
        return tokens


def _protect(text: str) -> str:
    """Replace periods that do not terminate sentences with a sentinel."""
    protected = _ABBREV_PATTERN.sub(lambda m: f"{m.group(1)}{_PERIOD_SENTINEL}", text)
    protected = _DECIMAL_PATTERN.sub(rf"\1{_PERIOD_SENTINEL}\2", protected)
    protected = _INITIAL_PATTERN.sub(rf"\1{_PERIOD_SENTINEL}", protected)
    return _ENUMERATION_PATTERN.sub(rf"\n\1{_PERIOD_SENTINEL} ", protected)


def _restore(text: str) -> str:
    return text.replace(_PERIOD_SENTINEL, ".")


def split_sentences(text: str) -> list[tuple[int, int]]:
    """Split text into sentence spans, returned as ``(start, end)`` offsets.

    Offsets index into the *original* string, so callers can slice source text directly
    and keep chunk character ranges accurate for citation — returning substrings instead
    would force a fragile re-search to recover positions.

    Newlines are treated as hard boundaries: clinical documents use line breaks
    structurally (one medication per line), and a line break is a stronger signal than
    any punctuation heuristic.
    """
    if not text.strip():
        return []

    spans: list[tuple[int, int]] = []
    line_start = 0
    for line in text.split("\n"):
        stripped_length = len(line)
        if line.strip():
            protected = _protect(line)
            cursor = 0
            for piece in _SENTENCE_BOUNDARY.split(protected):
                if not piece:
                    continue
                restored = _restore(piece)
                start = line.find(restored, cursor)
                if start == -1:
                    # Protection/restoration changed the text in a way that broke the
                    # positional match; fall back to the whole line so no content is lost.
                    spans.append((line_start, line_start + stripped_length))
                    break
                end = start + len(restored)
                if restored.strip():
                    spans.append((line_start + start, line_start + end))
                cursor = end
        line_start += stripped_length + 1

    return spans
