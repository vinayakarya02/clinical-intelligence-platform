"""Clinical text primitives shared across stages.

Small, dependency-free, and at the base of the module order. These live here rather than in
whichever stage happened to need them first: ``safety`` was importing a *private* regex out of
``validation`` and ``validation`` was importing a number parser out of ``reasoning``, which is
how two peers quietly become coupled.
"""

from __future__ import annotations

import re

__all__ = ["MEASUREMENT", "NUMBER", "extract_numbers"]

#: Numbers as written. Kept as strings by callers: "5.40" and "5.4" are the same number and
#: different transcriptions, and verification compares what was written to what the source said.
NUMBER = re.compile(r"\d+(?:\.\d+)?")

#: An analyte followed by its value, for detecting two sources disagreeing about one
#: measurement. Bounded to 20 characters so it does not pair an analyte with a number from the
#: next clause.
MEASUREMENT = re.compile(
    r"\b(potassium|sodium|creatinine|troponin|hemoglobin|glucose|inr|platelet|albumin)\b"
    r"[^.\n]{0,20}?(\d+(?:\.\d+)?)",
    re.I,
)


def extract_numbers(text: str) -> tuple[str, ...]:
    """Numbers appearing in ``text``, as written."""
    return tuple(NUMBER.findall(text))
