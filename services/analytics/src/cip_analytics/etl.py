"""The warehouse ETL: watermarked, incremental, de-identifying at load.

Three properties, each of which is the thing that breaks when it is missing.

**Watermarked and incremental.** A full reload of a clinical warehouse is hours of work that
contends with the systems being read. The watermark is the source's own monotonic cursor, never
wall-clock time — clocks on source systems disagree, and a time-based watermark silently skips
records written during the skew (docs/design/adr-0029-event-ordering.md made the same argument
for the event stream).

**Idempotent.** A load that is re-run from the same watermark must not duplicate rows. Rerunning
is normal — a crashed job, a fixed transform, a backfill — so the natural key of every fact is
declared and the loader deduplicates against what is already present.

**De-identified at load** (docs/design/adr-0033-deidentify-at-load.md). The warehouse does not
contain direct identifiers because nothing here writes them, and the salt that produces the
pseudonyms never enters the warehouse.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from cip_analytics.domain import AnalyticsError, SchemaError
from cip_analytics.warehouse import Warehouse
from cip_core.logging import get_logger

__all__ = [
    "CursorKind",
    "EtlError",
    "LoadRun",
    "LoadStatus",
    "Pipeline",
    "SourceBatch",
    "TableLoader",
    "Watermark",
    "WatermarkStore",
    "age_band",
    "postal_prefix",
    "pseudonym",
]

_log = get_logger(__name__)

#: Ages at or above this are banded together. Safe Harbor requires aggregating ages over 89, and
#: the warehouse's age band is the only age it holds.
_AGE_CAP = 90


class EtlError(AnalyticsError):
    """A load could not be completed."""


def pseudonym(value: str, *, salt: str, prefix: str = "") -> str:
    """A stable, non-reversible key for an identifier.

    Stable within a salt so a patient is one patient across facts and across loads — without
    that, every join in the warehouse is wrong. Non-reversible by anyone holding only the
    warehouse, because **the salt is never written to it**
    (docs/design/adr-0033-deidentify-at-load.md).
    """
    if not salt:
        raise EtlError(
            "pseudonymisation requires a salt; an unsalted digest of a medical record number "
            "is reversible by anyone who can enumerate record numbers, which is everyone"
        )
    digest = hashlib.blake2b(f"{salt}|{value}".encode(), digest_size=16).hexdigest()
    return f"{prefix}{digest}" if prefix else digest


def age_band(age_years: int | None, *, width: int = 5) -> str:
    """A quasi-identifying age band, capped.

    ``None`` becomes ``"unknown"`` rather than a band. Bucketing an unknown age into a real band
    invents a fact, and "unknown" is a legitimate analytic category.
    """
    if age_years is None or age_years < 0:
        return "unknown"
    if age_years >= _AGE_CAP:
        return f"{_AGE_CAP}+"
    low = (age_years // width) * width
    return f"{low}-{low + width - 1}"


def postal_prefix(postal_code: str | None, *, restricted: frozenset[str] = frozenset()) -> str:
    """Three digits at most, and nothing at all for a restricted area.

    Mirrors the Safe Harbor rule implemented in Phase 6: a three-digit area holding 20,000 people
    or fewer is suppressed entirely, because truncating a unit that is already small does not
    de-identify it.
    """
    if not postal_code:
        return "unknown"
    digits = "".join(c for c in postal_code if c.isdigit())[:3]
    if not digits:
        return "unknown"
    return "suppressed" if digits in restricted else digits


class LoadStatus(StrEnum):
    """How a load run ended."""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    PARTIAL = "partial"
    """Some rows were rejected. Distinct from failure because the loaded rows are usable and the
    rejected ones are a data-quality signal, not an outage."""

    @property
    def advances_watermark(self) -> bool:
        """Whether the watermark may move.

        ``PARTIAL`` does. Holding the watermark back because a handful of rows were malformed
        means the pipeline re-reads and re-rejects them forever and never makes progress; the
        rejects are recorded instead.
        """
        return self in (LoadStatus.SUCCEEDED, LoadStatus.PARTIAL)


class CursorKind(StrEnum):
    """How a source's cursor is ordered.

    Declared per source, because comparing cursors lexically is only correct for zero-padded
    or ISO-8601 values. A source emitting plain integers orders ``"10" < "9"`` lexically, so the
    watermark refuses to advance past 9 and the pipeline stops making progress **permanently** —
    a silent halt rather than an error.
    """

    LEXICAL = "lexical"
    """Zero-padded ids, ISO-8601 timestamps, ULIDs. String order is correct."""
    NUMERIC = "numeric"
    """Plain integers or decimals."""

    def is_after(self, candidate: str, current: str) -> bool:
        """Whether ``candidate`` is at or past ``current``."""
        if not current:
            return True
        if self is CursorKind.NUMERIC:
            try:
                return float(candidate) >= float(current)
            except ValueError:
                # A source that declared NUMERIC and emitted something else is a configuration
                # error. Refusing is safer than falling back to lexical, which would silently
                # reorder.
                raise EtlError(
                    f"cursor {candidate!r} is not numeric but this source declares a numeric "
                    "cursor; the pipeline cannot order its batches"
                ) from None
        return candidate >= current


@dataclass(frozen=True, slots=True)
class Watermark:
    """How far a source has been read.

    ``cursor`` is the source's own monotonic position — an event offset, a sequence number, a
    version counter. A string so a source can use whatever it has, ordered by its declared
    :class:`CursorKind` rather than assumed to be lexical.
    """

    source: str
    fact: str
    cursor: str = ""
    updated_at: dt.datetime | None = None
    rows_loaded: int = 0
    kind: CursorKind = CursorKind.LEXICAL

    @property
    def key(self) -> tuple[str, str]:
        return (self.source, self.fact)

    @property
    def is_initial(self) -> bool:
        return not self.cursor


class WatermarkStore:
    """Where watermarks live between runs.

    Separate from the warehouse: the watermark is pipeline state, not analytic data, and mixing
    them means a warehouse restore silently rewinds or fast-forwards the pipeline.
    """

    def __init__(self) -> None:
        self._marks: dict[tuple[str, str], Watermark] = {}

    def get(self, source: str, fact: str) -> Watermark:
        return self._marks.get((source, fact), Watermark(source=source, fact=fact))

    def advance(self, watermark: Watermark) -> None:
        current = self.get(watermark.source, watermark.fact)
        if (
            current.cursor
            and watermark.cursor
            and not watermark.kind.is_after(watermark.cursor, current.cursor)
        ):
            # Refused rather than accepted. A watermark going backwards silently reprocesses a
            # window, and because the loader is idempotent the reprocessing is invisible —
            # it just costs hours. A deliberate rewind uses reset().
            raise EtlError(
                f"watermark for {watermark.source}/{watermark.fact} would move backwards from "
                f"{current.cursor!r} to {watermark.cursor!r}; use reset() to reprocess"
            )
        self._marks[watermark.key] = watermark

    def reset(self, source: str, fact: str) -> None:
        """Deliberately rewind a source to the beginning, for a backfill or a transform fix."""
        self._marks.pop((source, fact), None)
        _log.info("etl.watermark_reset", source=source, fact=fact)

    def all(self) -> tuple[Watermark, ...]:
        return tuple(sorted(self._marks.values(), key=lambda w: w.key))


@dataclass(frozen=True, slots=True)
class SourceBatch:
    """One batch read from a source."""

    records: tuple[dict[str, Any], ...]
    next_cursor: str
    exhausted: bool = False

    @property
    def size(self) -> int:
        return len(self.records)


@dataclass(frozen=True, slots=True)
class TableLoader:
    """How one source populates one fact table.

    ``natural_key`` is what makes reruns idempotent. Without it a rerun appends a second copy of
    every row and every count doubles — a failure that looks like a business trend.
    """

    source: str
    fact: str
    natural_key: tuple[str, ...]
    transform: Callable[[dict[str, Any], str], dict[str, Any] | None]
    """``(source record, salt) -> fact row``, or ``None`` to skip the record. Skipping is not an
    error: a source legitimately carries records that do not belong in a given fact."""
    dimension_rows: Callable[[dict[str, Any], str], list[tuple[str, dict[str, Any]]]] | None = None
    """Dimension upserts this record implies, as ``(dimension name, row)``."""

    def __post_init__(self) -> None:
        if not self.natural_key:
            raise SchemaError(
                f"loader {self.source}->{self.fact} declares no natural key, so a rerun would "
                "duplicate every row and every count would double"
            )


@dataclass(frozen=True, slots=True)
class LoadRun:
    """What one execution of a loader did."""

    load_id: str
    source: str
    fact: str
    status: LoadStatus
    started_at: dt.datetime
    finished_at: dt.datetime | None = None
    rows_read: int = 0
    rows_loaded: int = 0
    rows_skipped: int = 0
    rows_duplicate: int = 0
    rows_rejected: int = 0
    cursor_from: str = ""
    cursor_to: str = ""
    ruleset_version: str = ""
    errors: tuple[str, ...] = ()

    @property
    def duration_ms(self) -> float:
        if self.finished_at is None:
            return 0.0
        return (self.finished_at - self.started_at).total_seconds() * 1000

    def to_json(self) -> dict[str, Any]:
        return {
            "loadId": self.load_id,
            "source": self.source,
            "fact": self.fact,
            "status": str(self.status),
            "rowsRead": self.rows_read,
            "rowsLoaded": self.rows_loaded,
            "rowsSkipped": self.rows_skipped,
            "rowsDuplicate": self.rows_duplicate,
            "rowsRejected": self.rows_rejected,
            "cursorFrom": self.cursor_from,
            "cursorTo": self.cursor_to,
            "rulesetVersion": self.ruleset_version,
            "durationMs": round(self.duration_ms, 3),
            "errors": list(self.errors[:20]),
        }

    def render(self) -> str:
        return (
            f"{self.source}->{self.fact} [{self.status.value}] read={self.rows_read} "
            f"loaded={self.rows_loaded} dup={self.rows_duplicate} "
            f"skip={self.rows_skipped} reject={self.rows_rejected} "
            f"cursor {self.cursor_from or 'start'}->{self.cursor_to}"
        )


class Pipeline:
    """Runs loaders against a warehouse.

    Holds the pseudonymisation salt, which is why it — and not the warehouse — is the component
    with a secret. Keeping the salt out of the warehouse is what makes the warehouse safe to
    replicate to an analytics environment with a broader audience.
    """

    def __init__(
        self,
        warehouse: Warehouse,
        *,
        salt: str,
        watermarks: WatermarkStore | None = None,
        ruleset_version: str = "safe-harbor-1.0.0",
        max_runs_retained: int = 5000,
    ) -> None:
        if not salt.strip():
            raise EtlError(
                "the pipeline requires a pseudonymisation salt; without one the warehouse keys "
                "are plain digests of medical record numbers and are trivially reversible"
            )
        self._warehouse = warehouse
        self._salt = salt
        self._watermarks = watermarks or WatermarkStore()
        self._ruleset_version = ruleset_version
        self._loaders: dict[tuple[str, str], TableLoader] = {}
        self._runs: list[LoadRun] = []
        self._max_runs = max_runs_retained
        self._seen_keys: dict[str, set[tuple[Any, ...]]] = {}

    @property
    def watermarks(self) -> WatermarkStore:
        return self._watermarks

    @property
    def ruleset_version(self) -> str:
        return self._ruleset_version

    def register(self, loader: TableLoader) -> None:
        if self._warehouse.schema.fact(loader.fact) is None:
            raise SchemaError(
                f"loader targets unknown fact {loader.fact!r}; a loader for a table that does "
                "not exist would silently load nothing"
            )
        key = (loader.source, loader.fact)
        if key in self._loaders:
            raise SchemaError(f"loader {loader.source}->{loader.fact} is already registered")
        self._loaders[key] = loader

    def loaders(self) -> tuple[TableLoader, ...]:
        return tuple(self._loaders.values())

    def run(
        self,
        source: str,
        fact: str,
        batches: Iterable[SourceBatch],
        *,
        at: dt.datetime | None = None,
    ) -> LoadRun:
        """Execute one loader over the batches a source yields."""
        loader = self._loaders.get((source, fact))
        if loader is None:
            raise EtlError(f"no loader registered for {source}->{fact}")

        started = at or dt.datetime.now(dt.UTC)
        load_id = f"load-{uuid.uuid4().hex[:12]}"
        watermark = self._watermarks.get(source, fact)
        seen = self._seen_keys.setdefault(fact, set())

        read = loaded = skipped = duplicate = rejected = 0
        errors: list[str] = []
        cursor = watermark.cursor
        pending: list[dict[str, Any]] = []

        for batch in batches:
            read += batch.size
            for record in batch.records:
                try:
                    row = loader.transform(record, self._salt)
                except Exception as exc:
                    rejected += 1
                    errors.append(f"{type(exc).__name__}: {exc}")
                    continue
                if row is None:
                    skipped += 1
                    continue

                key = tuple(row.get(column) for column in loader.natural_key)
                if any(part is None for part in key):
                    rejected += 1
                    errors.append(
                        f"row has no value for natural key {loader.natural_key}; it cannot be "
                        "deduplicated and a rerun would double it"
                    )
                    continue
                if key in seen:
                    duplicate += 1
                    continue
                seen.add(key)

                if loader.dimension_rows is not None:
                    for dimension_name, dimension_row in loader.dimension_rows(record, self._salt):
                        self._warehouse.upsert_dimension(dimension_name, dimension_row)

                pending.append(row)
            cursor = batch.next_cursor or cursor

        try:
            if pending:
                loaded = self._warehouse.append_facts(
                    fact,
                    pending,
                    load_id=load_id,
                    as_of=started,
                    ruleset_version=self._ruleset_version,
                )
        except SchemaError as exc:
            # A schema violation is not a bad record; it means the transform and the schema
            # disagree, and loading the rest would produce a half-populated table nobody can
            # interpret. The whole batch fails and the watermark does not move.
            run = LoadRun(
                load_id=load_id,
                source=source,
                fact=fact,
                status=LoadStatus.FAILED,
                started_at=started,
                finished_at=dt.datetime.now(dt.UTC),
                rows_read=read,
                rows_rejected=len(pending),
                cursor_from=watermark.cursor,
                cursor_to=watermark.cursor,
                ruleset_version=self._ruleset_version,
                errors=(str(exc),),
            )
            self._record(run)
            _log.error("etl.load_failed", source=source, fact=fact, error=str(exc)[:200])
            return run

        status = LoadStatus.PARTIAL if rejected else LoadStatus.SUCCEEDED
        run = LoadRun(
            load_id=load_id,
            source=source,
            fact=fact,
            status=status,
            started_at=started,
            finished_at=dt.datetime.now(dt.UTC),
            rows_read=read,
            rows_loaded=loaded,
            rows_skipped=skipped,
            rows_duplicate=duplicate,
            rows_rejected=rejected,
            cursor_from=watermark.cursor,
            cursor_to=cursor,
            ruleset_version=self._ruleset_version,
            errors=tuple(errors),
        )
        self._record(run)

        if status.advances_watermark and cursor:
            self._watermarks.advance(
                Watermark(
                    source=source,
                    fact=fact,
                    cursor=cursor,
                    updated_at=started,
                    rows_loaded=watermark.rows_loaded + loaded,
                )
            )
        _log.info("etl.loaded", detail=run.render())
        return run

    def _record(self, run: LoadRun) -> None:
        self._runs.append(run)
        # Bounded, oldest first. Failed runs are retained preferentially: a successful run is
        # visible in the data, and a failed one is only visible here.
        if len(self._runs) > self._max_runs:
            failures = [r for r in self._runs if r.status is LoadStatus.FAILED]
            others = [r for r in self._runs if r.status is not LoadStatus.FAILED]
            keep = max(0, self._max_runs - len(failures))
            self._runs = failures + others[-keep:] if keep else failures[-self._max_runs :]

    def runs(self, *, fact: str | None = None) -> tuple[LoadRun, ...]:
        return tuple(r for r in self._runs if fact is None or r.fact == fact)

    def last_run(self, source: str, fact: str) -> LoadRun | None:
        for run in reversed(self._runs):
            if run.source == source and run.fact == fact:
                return run
        return None

    def statistics(self) -> dict[str, Any]:
        return {
            "loaders": len(self._loaders),
            "runs": len(self._runs),
            "failed": sum(1 for r in self._runs if r.status is LoadStatus.FAILED),
            "partial": sum(1 for r in self._runs if r.status is LoadStatus.PARTIAL),
            "rowsLoaded": sum(r.rows_loaded for r in self._runs),
            "rowsRejected": sum(r.rows_rejected for r in self._runs),
            "duplicatesSuppressed": sum(r.rows_duplicate for r in self._runs),
            "watermarks": {f"{w.source}/{w.fact}": w.cursor for w in self._watermarks.all()},
        }


def batched(
    records: Sequence[dict[str, Any]], *, size: int, cursor_field: str
) -> list[SourceBatch]:
    """Split records into batches, taking the cursor from each batch's last record.

    A helper for sources that hand over a list. A real source streams and yields its own
    batches; this exists so tests and the demo do not have to reimplement the shape.
    """
    if size < 1:
        raise EtlError("batch size must be at least 1")
    out: list[SourceBatch] = []
    for start in range(0, len(records), size):
        window = records[start : start + size]
        cursor = str(window[-1].get(cursor_field, "")) if window else ""
        out.append(
            SourceBatch(
                records=tuple(window),
                next_cursor=cursor,
                exhausted=start + size >= len(records),
            )
        )
    return out


@dataclass(slots=True)
class DimensionBuilder:
    """Builds conformed dimension rows and hands back their surrogate keys.

    Surrogate keys are generated here rather than taken from the source, so a renamed
    organisation or a retired code does not silently re-parent the facts that pointed at it.
    """

    salt: str
    _issued: dict[tuple[str, str], str] = field(default_factory=dict)

    def key_for(self, dimension: str, natural: str) -> str:
        cached = self._issued.get((dimension, natural))
        if cached is not None:
            return cached
        issued = pseudonym(f"{dimension}|{natural}", salt=self.salt, prefix="k")
        self._issued[(dimension, natural)] = issued
        return issued

    def date_key(self, when: dt.date) -> int:
        return when.year * 10000 + when.month * 100 + when.day

    def date_row(self, when: dt.date) -> dict[str, Any]:
        return {
            "date_key": self.date_key(when),
            "date": when.isoformat(),
            "month": f"{when.year:04d}-{when.month:02d}",
            "quarter": f"{when.year:04d}-Q{(when.month - 1) // 3 + 1}",
            "year": when.year,
            "day_of_week": when.strftime("%A"),
            "is_weekend": when.weekday() >= 5,
        }
