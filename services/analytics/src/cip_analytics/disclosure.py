"""Statistical disclosure control.

Primary suppression withholds a cell whose subject count is below the threshold. Applied alone —
which is how it is usually applied — it does not work, because a suppressed cell sitting beside
published cells and a published total is recoverable by subtraction. That is the first thing
anyone does with a suppressed table (docs/design/adr-0036-complementary-suppression.md).

So suppression is a **set operation over the whole result**, not a per-cell decision:

1. primary — cells below the threshold
2. complementary — enough additional cells that no suppressed value is pinned down
3. the total — withheld when publishing it would permit the subtraction
4. refusal — when no combination is safe, nothing is published, with the reason

**This is not differential privacy.** It is deterministic cell suppression, and its guarantee is
correspondingly weaker: it defends against the arithmetic above and against small-cell
identification, and it does not defend against an attacker with strong auxiliary information or
against differencing across many correlated queries.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from cip_analytics.domain import DisclosurePolicy, SuppressionError
from cip_core.logging import get_logger

__all__ = [
    "Cell",
    "SuppressionOutcome",
    "SuppressionReason",
    "apply_disclosure_control",
]

_log = get_logger(__name__)

#: A group needs at least this many suppressed cells before its suppressed values stop being
#: individually recoverable from a published total. With one, subtraction gives the value
#: exactly.
_MINIMUM_SUPPRESSED_PER_GROUP = 2


class SuppressionReason:
    """Why a cell was withheld. Strings rather than an enum because they are shown to analysts
    and read as sentences."""

    PRIMARY = "below the minimum cell size"
    COMPLEMENTARY = "withheld to protect another suppressed cell from subtraction"
    TOTAL = "total withheld because publishing it would reveal a suppressed cell"


@dataclass(slots=True)
class Cell:
    """One row of a result before disclosure control.

    ``subjects`` is the count of distinct people behind the cell, which is **not** always the
    value. A cell reporting an average length of stay over four patients has a value near 5 and
    a subject count of 4; suppression cares about the 4.
    """

    group: tuple[Any, ...]
    value: float | int | None
    subjects: int
    suppressed: bool = False
    reason: str = ""

    @property
    def is_published(self) -> bool:
        return not self.suppressed

    def to_json(self, labels: tuple[str, ...]) -> dict[str, Any]:
        payload: dict[str, Any] = dict(zip(labels, self.group, strict=False))
        # A suppressed cell reports None, never its value. Returning the value with a
        # "suppressed" flag beside it and expecting the client to hide it is how suppressed
        # numbers end up in a CSV export.
        payload["value"] = None if self.suppressed else self.value
        if self.suppressed:
            payload["suppressed"] = True
            payload["suppressionReason"] = self.reason
        return payload


@dataclass(frozen=True, slots=True)
class SuppressionOutcome:
    """The result of applying disclosure control."""

    cells: tuple[Cell, ...]
    total: float | int | None
    total_suppressed: bool
    primary_suppressed: int
    complementary_suppressed: int
    threshold: int

    @property
    def suppressed_count(self) -> int:
        return self.primary_suppressed + self.complementary_suppressed

    @property
    def any_suppressed(self) -> bool:
        return self.suppressed_count > 0 or self.total_suppressed

    def note(self) -> str:
        """What a consumer must be told.

        A reader who does not know a table was suppressed will treat the visible cells as the
        whole picture and compute their own — wrong — total.
        """
        if not self.any_suppressed:
            return ""
        parts = [
            f"{self.suppressed_count} cell(s) withheld under a minimum cell size of "
            f"{self.threshold}"
        ]
        if self.complementary_suppressed:
            parts.append(
                f"{self.complementary_suppressed} of them to prevent recovery by subtraction"
            )
        if self.total_suppressed:
            parts.append("the total is withheld for the same reason")
        parts.append("visible cells do not sum to the population")
        return "; ".join(parts)

    def to_json(self, labels: tuple[str, ...]) -> dict[str, Any]:
        return {
            "rows": [c.to_json(labels) for c in self.cells],
            "total": None if self.total_suppressed else self.total,
            "totalSuppressed": self.total_suppressed,
            "suppression": {
                "threshold": self.threshold,
                "primary": self.primary_suppressed,
                "complementary": self.complementary_suppressed,
                "note": self.note(),
            },
        }


def _recoverable(cells: list[Cell], total: float | int | None) -> bool:
    """Whether any suppressed cell can be derived from what would be published.

    Two ways, and both are arithmetic rather than attack:

    - exactly one cell suppressed while the total is published: subtraction gives it exactly
    - the residual is zero: every suppressed cell must be zero
    """
    suppressed = [c for c in cells if c.suppressed]
    if not suppressed or total is None:
        return False
    if len(suppressed) < _MINIMUM_SUPPRESSED_PER_GROUP:
        return True
    published_sum = sum(c.value or 0 for c in cells if c.is_published)
    residual = (total or 0) - published_sum
    return residual <= 0


def apply_disclosure_control(
    cells: list[Cell],
    *,
    policy: DisclosurePolicy,
    total: float | int | None = None,
    additive: bool = True,
) -> SuppressionOutcome:
    """Suppress a result so nothing withheld can be recovered from what is published.

    ``additive`` says whether the total is the sum of the cells. For a non-additive measure — a
    rate, an average, a distinct count — it is not, so subtraction does not recover anything and
    only primary suppression applies. Treating a rate table as additive would suppress cells for
    no benefit; treating a count table as non-additive would leave it recoverable.
    """
    working = [Cell(group=c.group, value=c.value, subjects=c.subjects) for c in cells]

    primary = 0
    for cell in working:
        if 0 < cell.subjects < policy.minimum_cell_size:
            cell.suppressed = True
            cell.reason = SuppressionReason.PRIMARY
            primary += 1

    complementary = 0
    total_suppressed = False

    if primary and additive:
        # Only additive measures are recoverable by subtraction. Everything below is about
        # closing that arithmetic, so it does not apply to rates or averages.
        while _recoverable(working, total if not total_suppressed else None):
            candidates = [c for c in working if c.is_published and c.value is not None]
            if not candidates:
                # Nothing left to suppress. Withholding the total is the only remaining move.
                if policy.suppress_totals_when_needed:
                    total_suppressed = True
                    break
                raise SuppressionError(
                    "the result cannot be made safe: every cell is already suppressed and this "
                    "policy forbids withholding the total, so the suppressed values remain "
                    "recoverable by subtraction"
                )
            # Suppress the smallest publishable cell. Smallest loses the least information, and
            # it is also the cell closest to the threshold and so the least costly to withhold.
            victim = min(candidates, key=lambda c: (c.value or 0, c.group))
            victim.suppressed = True
            victim.reason = SuppressionReason.COMPLEMENTARY
            complementary += 1

    if total_suppressed and not policy.suppress_totals_when_needed:  # pragma: no cover
        raise SuppressionError("total suppression required but forbidden by policy")

    # Final check: after complementary suppression the residual may still pin the values down.
    # Withhold the total rather than publish a table that leaks.
    if (
        (primary or complementary)
        and additive
        and not total_suppressed
        and _recoverable(working, total)
    ):
        if not policy.suppress_totals_when_needed:
            raise SuppressionError(
                "the result cannot be made safe without withholding the total, which this "
                "policy forbids"
            )
        total_suppressed = True

    if primary or complementary or total_suppressed:
        _log.info(
            "disclosure.suppressed",
            primary=primary,
            complementary=complementary,
            total_suppressed=total_suppressed,
            threshold=policy.minimum_cell_size,
        )

    return SuppressionOutcome(
        cells=tuple(working),
        total=total,
        total_suppressed=total_suppressed,
        primary_suppressed=primary,
        complementary_suppressed=complementary,
        threshold=policy.minimum_cell_size,
    )


@dataclass(slots=True)
class QuasiIdentifierBudget:
    """Counts quasi-identifying dimensions in a grouping.

    Two are usually safe; three single out a person in a small population. Enforced at query
    time rather than trusted to template authors, because the combination that identifies
    somebody is a property of the *request*, not of any single template.
    """

    maximum: int
    used: list[str] = field(default_factory=list)

    def add(self, dimension: str) -> None:
        self.used.append(dimension)

    @property
    def exceeded(self) -> bool:
        return len(self.used) > self.maximum

    def refusal(self) -> str:
        return (
            f"grouping by {', '.join(self.used)} combines {len(self.used)} quasi-identifying "
            f"dimensions and the policy permits {self.maximum}. Each is harmless alone; "
            "together they single out individuals."
        )
