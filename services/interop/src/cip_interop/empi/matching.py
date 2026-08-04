"""Probabilistic record matching.

Fellegi–Sunter: each field contributes ``log2(m/u)`` when it agrees and ``log2((1-m)/(1-u))``
when it disagrees, where ``m`` is the probability the field agrees given a true match and ``u``
the probability it agrees by chance. The weights sum, and the sum is compared against two
thresholds (docs/design/adr-0027-empi-review-not-automerge.md).

Three decisions here are worth more than the arithmetic.

**Missing is neutral, not disagreement.** A field absent on either side contributes zero. A
record with no recorded middle name does not *disagree* with one that has it, and scoring it as
disagreement penalises every record from a system that collects less — which is most of them.

**Partial agreement is interpolated, not rounded.** "Jonathan" against "Jon" is neither
agreement nor disagreement, and forcing it to one of them either merges people who share a
common surname or splits one person across a nickname. The similarity is mapped linearly
between the two weights.

**m and u are configuration.** They are population properties: a surname's discriminating power
differs by region, and the ``u`` for a common surname is not the ``u`` for a rare one. Defaults
ship, and are documented as defaults rather than as truth.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from enum import StrEnum

from cip_interop.domain import PersonRecord, normalise_name_token

__all__ = [
    "FieldComparison",
    "FieldWeight",
    "MatchScore",
    "MatchZone",
    "MatchingModel",
    "default_model",
    "jaro_winkler",
]


def jaro_winkler(left: str, right: str, *, prefix_scale: float = 0.1) -> float:
    """Similarity in ``[0, 1]``.

    Jaro–Winkler rather than edit distance because it weights **common prefixes**, and the
    dominant name error in registration data is a truncation or a transposition near the end
    ("Robertson"/"Roberston", "Katherine"/"Kathrine"), not a uniform sprinkle of edits.
    """
    if left == right:
        return 1.0
    if not left or not right:
        return 0.0

    window = max(len(left), len(right)) // 2 - 1
    if window < 0:
        window = 0

    left_matches = [False] * len(left)
    right_matches = [False] * len(right)
    matches = 0
    for i, char in enumerate(left):
        start = max(0, i - window)
        end = min(i + window + 1, len(right))
        for j in range(start, end):
            if right_matches[j] or right[j] != char:
                continue
            left_matches[i] = right_matches[j] = True
            matches += 1
            break
    if matches == 0:
        return 0.0

    transpositions = 0
    k = 0
    for i, matched in enumerate(left_matches):
        if not matched:
            continue
        while not right_matches[k]:
            k += 1
        if left[i] != right[k]:
            transpositions += 1
        k += 1
    transpositions //= 2

    jaro = (matches / len(left) + matches / len(right) + (matches - transpositions) / matches) / 3.0

    prefix = 0
    for a, b in zip(left, right, strict=False):
        if a != b or prefix == 4:
            break
        prefix += 1
    return jaro + prefix * prefix_scale * (1 - jaro)


class MatchZone(StrEnum):
    """Where a score falls.

    Three zones, not two. A two-zone matcher must put every ambiguous pair somewhere, and both
    choices are harmful: auto-merging two people produces a chart with someone else's
    allergies, auto-splitting hides half a history.
    """

    MATCH = "match"
    REVIEW = "review"
    NON_MATCH = "non_match"

    @property
    def is_actionable_without_a_human(self) -> bool:
        return self is not MatchZone.REVIEW


class Comparator(StrEnum):
    """How a field is compared."""

    EXACT = "exact"
    """Folded string equality. For codes and identifiers."""
    SIMILARITY = "similarity"
    """Jaro–Winkler with partial credit. For names."""
    DATE = "date"
    """Exact, with a transposition allowance — see :func:`_compare_date`."""
    NUMERIC_TAIL = "numeric_tail"
    """Digits only, compared on the tail. For phone numbers."""


@dataclass(frozen=True, slots=True)
class FieldWeight:
    """One field's contribution to the score.

    ``m`` and ``u`` are probabilities in ``(0, 1)``, exclusive. A ``u`` of zero would give an
    infinite agreement weight, meaning one field agreement alone forces a match — which is
    exactly the bug where two people sharing a birthday get merged.
    """

    name: str
    m: float
    u: float
    comparator: Comparator = Comparator.EXACT
    agreement_threshold: float = 0.92
    """Similarity at or above which a field counts as full agreement."""
    partial_threshold: float = 0.70
    """Similarity below which a field counts as full disagreement. Between the two, the weight
    is interpolated."""

    correlation_group: str = ""
    """Fields that are not conditionally independent of each other.

    Fellegi–Sunter sums field weights, which is valid only if the fields are independent given
    match status. Address and telephone are not: people in one household share both. Summing
    them counts one fact — "these records share a household" — twice, and two counts of it
    outweigh disagreement on given name, birth date, and sex combined. Members of a group
    contribute only their strongest single weight (see :meth:`MatchingModel.compare`).
    """

    def __post_init__(self) -> None:
        for label, value in (("m", self.m), ("u", self.u)):
            if not 0.0 < value < 1.0:
                raise ValueError(
                    f"FieldWeight '{self.name}' has {label}={value}; probabilities must be "
                    "strictly between 0 and 1. A boundary value produces an infinite weight, "
                    "which lets one field decide a match on its own."
                )
        if self.m <= self.u:
            raise ValueError(
                f"FieldWeight '{self.name}' has m={self.m} <= u={self.u}, so agreement on it "
                "is evidence *against* a match. Either the probabilities are swapped or the "
                "field is not discriminating."
            )
        if self.partial_threshold > self.agreement_threshold:
            raise ValueError(
                f"FieldWeight '{self.name}': partial_threshold exceeds agreement_threshold"
            )

    @property
    def agreement_weight(self) -> float:
        return math.log2(self.m / self.u)

    @property
    def disagreement_weight(self) -> float:
        return math.log2((1 - self.m) / (1 - self.u))

    def weight_for(self, similarity: float) -> float:
        """The contribution for an observed similarity.

        Interpolated linearly across the partial band. Values outside the band saturate.
        """
        if similarity >= self.agreement_threshold:
            return self.agreement_weight
        if similarity <= self.partial_threshold:
            return self.disagreement_weight
        span = self.agreement_threshold - self.partial_threshold
        position = (similarity - self.partial_threshold) / span
        return self.disagreement_weight + position * (
            self.agreement_weight - self.disagreement_weight
        )


@dataclass(frozen=True, slots=True)
class FieldComparison:
    """What one field contributed, and why."""

    field: str
    similarity: float | None
    weight: float
    left: str = ""
    right: str = ""
    correlation_group: str = ""

    @property
    def missing(self) -> bool:
        return self.similarity is None

    def render(self) -> str:
        if self.missing:
            return f"{self.field}: not compared (absent on one side), weight 0.00"
        group = f" [group {self.correlation_group}]" if self.correlation_group else ""
        return (
            f"{self.field}: {self.left!r} vs {self.right!r} "
            f"similarity {self.similarity:.2f}, weight {self.weight:+.2f}{group}"
        )


@dataclass(frozen=True, slots=True)
class MatchScore:
    """A decomposed score.

    Every comparison is retained, including the ones that contributed nothing, so a reviewer
    deciding a queued pair sees what the algorithm actually looked at rather than a number.
    """

    total: float
    zone: MatchZone
    comparisons: tuple[FieldComparison, ...] = ()
    deterministic_override: str = ""
    """Set when a nationally-unique identifier promoted the pair. Named, because "why is this a
    match" must be answerable and "the score said so" is not an answer when the score did not."""

    @property
    def compared_fields(self) -> int:
        return sum(1 for c in self.comparisons if not c.missing)

    @property
    def missing_fields(self) -> tuple[str, ...]:
        return tuple(c.field for c in self.comparisons if c.missing)

    def render(self) -> str:
        lines = [f"score {self.total:+.2f} -> {self.zone.value}"]
        if self.deterministic_override:
            lines.append(f"  promoted by {self.deterministic_override}")
        lines.extend(f"  {c.render()}" for c in self.comparisons)
        return "\n".join(lines)


def _folded(value: str) -> str:
    return normalise_name_token(value)


def _compare_date(left: dt.date | None, right: dt.date | None) -> float | None:
    """Compare two birth dates.

    Exact match scores 1.0. A **day/month transposition** scores 0.8 rather than 0: it is one
    of the most common registration errors, especially where systems disagree about US and
    international date order, and treating it as full disagreement splits the same person at
    every boundary. It is not scored as agreement either — it might be two siblings.
    """
    if left is None or right is None:
        return None
    if left == right:
        return 1.0
    if left.year == right.year and left.day == right.month and left.month == right.day:
        return 0.80
    if left.year == right.year and left.month == right.month:
        return 0.55
    if abs(left.year - right.year) <= 1 and (left.month, left.day) == (right.month, right.day):
        return 0.55
    return 0.0


def _compare_numeric_tail(left: str, right: str) -> float | None:
    if not left or not right:
        return None
    return 1.0 if left == right else 0.0


@dataclass(frozen=True, slots=True)
class MatchingModel:
    """The configured matcher.

    Thresholds are in log2 weight units. They are set from the desired error rates for a
    population and are *not* transferable between deployments, which is why they are
    constructor arguments with documented defaults rather than module constants.
    """

    weights: tuple[FieldWeight, ...]
    upper_threshold: float
    lower_threshold: float
    #: Identifier systems whose agreement promotes a pair straight to ``MATCH``. Promotion only:
    #: a missing identifier can never demote a probabilistic match, because most records lack
    #: one and demoting on absence would break matching for the majority.
    deterministic_systems: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if self.upper_threshold <= self.lower_threshold:
            raise ValueError(
                f"upper_threshold ({self.upper_threshold}) must exceed lower_threshold "
                f"({self.lower_threshold}); with them inverted or equal the review zone "
                "vanishes and every ambiguous pair is auto-decided"
            )
        if not self.weights:
            raise ValueError("MatchingModel requires at least one field weight")

    def zone_for(self, total: float) -> MatchZone:
        if total >= self.upper_threshold:
            return MatchZone.MATCH
        if total <= self.lower_threshold:
            return MatchZone.NON_MATCH
        return MatchZone.REVIEW

    def compare(self, left: PersonRecord, right: PersonRecord) -> MatchScore:
        """Score one pair."""
        comparisons: list[FieldComparison] = []
        total = 0.0
        groups: dict[str, list[float]] = {}

        left_name = left.primary_name
        right_name = right.primary_name

        for weight in self.weights:
            similarity, left_text, right_text = self._similarity(
                weight, left, right, left_name, right_name
            )
            if similarity is None:
                comparisons.append(FieldComparison(field=weight.name, similarity=None, weight=0.0))
                continue
            contribution = weight.weight_for(similarity)
            if weight.correlation_group:
                groups.setdefault(weight.correlation_group, []).append(contribution)
            else:
                total += contribution
            comparisons.append(
                FieldComparison(
                    field=weight.name,
                    similarity=similarity,
                    weight=contribution,
                    left=left_text,
                    right=right_text,
                    correlation_group=weight.correlation_group,
                )
            )

        # A correlated group contributes its single strongest weight, by magnitude. Summing
        # them would count one underlying fact — a shared household — as many times as there
        # are fields that reveal it.
        for members in groups.values():
            total += max(members, key=abs)

        override = self._deterministic_agreement(left, right)
        zone = self.zone_for(total)
        if override and zone is not MatchZone.MATCH:
            zone = MatchZone.MATCH

        return MatchScore(
            total=round(total, 4),
            zone=zone,
            comparisons=tuple(comparisons),
            deterministic_override=override,
        )

    def _deterministic_agreement(self, left: PersonRecord, right: PersonRecord) -> str:
        """A shared nationally-unique identifier, if any."""
        right_keys = {
            i.key: i
            for i in right.matching_identifiers()
            if i.is_nationally_unique or i.system.lower() in self.deterministic_systems
        }
        for identifier in left.matching_identifiers():
            if not (
                identifier.is_nationally_unique
                or identifier.system.lower() in self.deterministic_systems
            ):
                continue
            if identifier.key in right_keys:
                return f"identifier {identifier.render()}"
        return ""

    def _similarity(
        self,
        weight: FieldWeight,
        left: PersonRecord,
        right: PersonRecord,
        left_name: object,
        right_name: object,
    ) -> tuple[float | None, str, str]:
        name = weight.name

        if name == "family_name":
            a = _folded(getattr(left_name, "family", "") or "")
            b = _folded(getattr(right_name, "family", "") or "")
            if not a or not b:
                return None, a, b
            return jaro_winkler(a, b), a, b

        if name == "given_name":
            a_parts = getattr(left_name, "comparison_given", ()) or ()
            b_parts = getattr(right_name, "comparison_given", ()) or ()
            if not a_parts or not b_parts:
                return None, "", ""
            a, b = a_parts[0], b_parts[0]
            return jaro_winkler(a, b), a, b

        if name == "birth_date":
            similarity = _compare_date(left.birth_date, right.birth_date)
            return (
                similarity,
                left.birth_date.isoformat() if left.birth_date else "",
                right.birth_date.isoformat() if right.birth_date else "",
            )

        if name == "sex":
            # Unknown participates in no comparison. Treating it as a disagreement would
            # penalise every record from a system that does not collect it.
            if left.sex.value == "unknown" or right.sex.value == "unknown":
                return None, left.sex.value, right.sex.value
            return (
                1.0 if left.sex is right.sex else 0.0,
                left.sex.value,
                right.sex.value,
            )

        if name == "postal_code":
            a = left.addresses[0].comparison_postal if left.addresses else ""
            b = right.addresses[0].comparison_postal if right.addresses else ""
            if not a or not b:
                return None, a, b
            return (1.0 if a == b else 0.0), a, b

        if name == "phone":
            a = next((t.comparison_value for t in left.telecom if t.system.lower() == "phone"), "")
            b = next((t.comparison_value for t in right.telecom if t.system.lower() == "phone"), "")
            similarity = _compare_numeric_tail(a, b)
            return similarity, a, b

        return None, "", ""


def default_model() -> MatchingModel:
    """A starting configuration.

    **These are defaults, not truth.** The ``m`` and ``u`` values are plausible for a general
    population and must be estimated from the deployment's own data — ideally by
    expectation-maximisation over unlabelled pairs, or from a manually adjudicated sample.
    Shipping them unchanged means matching on somebody else's population.

    The thresholds put roughly a two-field-agreement gap between match and non-match, leaving a
    review band wide enough that the ambiguous cases land in it rather than being decided.
    """
    return MatchingModel(
        weights=(
            FieldWeight("family_name", m=0.92, u=0.02, comparator=Comparator.SIMILARITY),
            FieldWeight("given_name", m=0.90, u=0.05, comparator=Comparator.SIMILARITY),
            FieldWeight(
                "birth_date",
                m=0.95,
                u=0.0003,
                comparator=Comparator.DATE,
                agreement_threshold=0.99,
                partial_threshold=0.50,
            ),
            FieldWeight("sex", m=0.98, u=0.50, agreement_threshold=0.99, partial_threshold=0.99),
            # Address and telephone are one fact about a household, not two about a person.
            # Grouped so they cannot jointly outweigh disagreement on name, birth date, and sex.
            FieldWeight(
                "postal_code",
                m=0.75,
                u=0.01,
                agreement_threshold=0.99,
                partial_threshold=0.99,
                correlation_group="household",
            ),
            FieldWeight(
                "phone",
                m=0.70,
                u=0.002,
                comparator=Comparator.NUMERIC_TAIL,
                agreement_threshold=0.99,
                partial_threshold=0.99,
                correlation_group="household",
            ),
        ),
        upper_threshold=16.0,
        lower_threshold=6.0,
    )


@dataclass(frozen=True, slots=True)
class BlockingStrategy:
    """How candidate pairs are narrowed before scoring.

    All-pairs comparison is quadratic and unusable past a few thousand records. Blocking makes
    it tractable and **loses recall by construction**: two records that share no blocking key
    are never compared, however similar they are.

    That cost is declared rather than hidden. Several independent keys are used precisely so a
    single field error does not remove a pair from consideration entirely — a typo'd surname
    still blocks on birth date.
    """

    name: str
    description: str
    recall_note: str

    def key(self, record: PersonRecord) -> str:
        if self.name == "birth_date":
            return record.birth_date.isoformat() if record.birth_date else ""
        if self.name == "family_soundex":
            name = record.primary_name
            folded = _folded(name.family) if name else ""
            return folded[:4] if folded else ""
        if self.name == "given_family_initial":
            name = record.primary_name
            if not name:
                return ""
            family = _folded(name.family)
            given = name.comparison_given
            return f"{family[:1]}{given[0][:1] if given else ''}" if family else ""
        if self.name == "postal_code":
            return record.addresses[0].comparison_postal if record.addresses else ""
        return ""


DEFAULT_BLOCKING: tuple[BlockingStrategy, ...] = (
    BlockingStrategy(
        name="birth_date",
        description="Exact birth date",
        recall_note="Misses pairs where either birth date is absent or mistyped",
    ),
    BlockingStrategy(
        name="family_soundex",
        description="First four characters of the folded family name",
        recall_note="Misses pairs whose surnames differ in the first four characters — "
        "including many marriage-related name changes",
    ),
    BlockingStrategy(
        name="postal_code",
        description="Postal code",
        recall_note="Misses pairs where either has moved or has no recorded address",
    ),
    BlockingStrategy(
        name="given_family_initial",
        description="Family and given initials",
        recall_note="Coarse; included so a pair with one badly-corrupted field still blocks "
        "somewhere",
    ),
)


@dataclass(slots=True)
class BlockingIndex:
    """Inverted index from blocking key to record ids.

    ``max_bucket_size`` is a correctness control, not a tuning knob. A blocking key whose
    bucket holds a large fraction of the index has stopped discriminating — every record
    recorded as born on 1 January because the birth date was unknown, or a synthetic population
    whose surnames all share a prefix — and consulting it makes matching quadratic again.

    An over-large bucket is **skipped and counted**, not silently truncated. Truncating loses
    recall invisibly; skipping loses the same recall but says so, and the count is the signal
    that a blocking strategy needs rethinking for this population.
    """

    strategies: tuple[BlockingStrategy, ...] = DEFAULT_BLOCKING
    max_bucket_size: int = 100
    """A bucket holding more than this is not narrowing anything.

    100 rather than a larger number because the cost is quadratic in bucket size: at 500 the
    matcher spends 50ms per ingest and throughput collapses to double digits per second, which
    is not a viable rate for a hospital ADT feed. Measured, not guessed — the first load
    simulation ran at 15 messages per second for exactly this reason."""
    _buckets: dict[tuple[str, str], set[str]] = field(default_factory=dict)
    _skipped: dict[str, int] = field(default_factory=dict)

    def add(self, record: PersonRecord) -> None:
        for strategy in self.strategies:
            key = strategy.key(record)
            if key:
                self._buckets.setdefault((strategy.name, key), set()).add(record.record_id)

    def remove(self, record: PersonRecord) -> None:
        for strategy in self.strategies:
            key = strategy.key(record)
            bucket = self._buckets.get((strategy.name, key))
            if bucket:
                bucket.discard(record.record_id)

    def candidates(self, record: PersonRecord) -> set[str]:
        found: set[str] = set()
        for strategy in self.strategies:
            key = strategy.key(record)
            if not key:
                continue
            bucket = self._buckets.get((strategy.name, key), set())
            if len(bucket) > self.max_bucket_size:
                self._skipped[strategy.name] = self._skipped.get(strategy.name, 0) + 1
                continue
            found |= bucket
        found.discard(record.record_id)
        return found

    def degenerate_strategies(self) -> dict[str, int]:
        """How often each strategy was skipped for being too broad.

        A non-zero count means that key is not discriminating in this population, and the
        matching it would have contributed is being lost. It belongs on an operational
        dashboard, not in a log nobody reads.
        """
        return dict(self._skipped)

    def bucket_sizes(self) -> dict[str, int]:
        """Largest bucket per strategy.

        A monitored number: a blocking key whose largest bucket is enormous (everyone born on
        1 January, which is what many systems record for an unknown birth date) has stopped
        narrowing anything and is quietly making the matcher quadratic again.
        """
        largest: dict[str, int] = {}
        for (strategy, _), bucket in self._buckets.items():
            largest[strategy] = max(largest.get(strategy, 0), len(bucket))
        return largest
