"""Metric registry with Prometheus exposition.

A small, dependency-free implementation of the three instrument types this platform needs —
counter, gauge, histogram — plus the text exposition format. Deliberately not
``prometheus_client``: metric *cardinality* is the failure mode that takes down a monitoring
stack, and owning the registry means the label-cardinality guard below is enforceable rather
than advisory.

The guard is the point. An unbounded label — a patient id, a query string, a correlation id —
produces a new time series per value, and a Prometheus instance ingesting a series per request
falls over quietly and takes the alerting with it. Every label value is checked against a
per-metric budget, and exceeding it is refused loudly rather than accepted quietly.
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field

from cip_core.logging import get_logger

__all__ = [
    "Counter",
    "Gauge",
    "Histogram",
    "MetricRegistry",
    "MetricsError",
]

_log = get_logger(__name__)

#: Buckets in seconds, spanning a cached hit to a slow model call. Chosen so the SLO
#: boundaries an operator actually alerts on (100 ms, 1 s, 5 s) are bucket edges — a
#: percentile interpolated across a bucket that straddles the threshold is not evidence.
DEFAULT_BUCKETS: tuple[float, ...] = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
    60.0,
)

#: Distinct label-value combinations one metric may have before it is refused. A tenant fleet
#: plus a handful of statuses fits comfortably; a patient id does not, which is the point.
DEFAULT_CARDINALITY_LIMIT = 2000


class MetricsError(RuntimeError):
    """A metric was used in a way that would damage the monitoring stack."""


def _labels_key(labels: dict[str, str]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted(labels.items()))


@dataclass
class _Series:
    """One metric's values, keyed by label set."""

    name: str
    kind: str
    help_text: str
    label_names: tuple[str, ...]
    cardinality_limit: int
    values: dict[tuple[tuple[str, str], ...], float] = field(default_factory=dict)
    buckets: tuple[float, ...] = ()
    bucket_counts: dict[tuple[tuple[str, str], ...], list[int]] = field(default_factory=dict)
    sums: dict[tuple[tuple[str, str], ...], float] = field(default_factory=dict)
    counts: dict[tuple[tuple[str, str], ...], int] = field(default_factory=dict)

    def check(self, labels: dict[str, str]) -> tuple[tuple[str, str], ...]:
        """Validate a label set and return its key."""
        if set(labels) != set(self.label_names):
            raise MetricsError(
                f"Metric '{self.name}' expects labels {sorted(self.label_names)}, "
                f"got {sorted(labels)}"
            )
        key = _labels_key(labels)
        if key not in self.values and key not in self.counts:
            existing = len(self.values) + len(self.counts)
            if existing >= self.cardinality_limit:
                raise MetricsError(
                    f"Metric '{self.name}' would exceed its cardinality limit of "
                    f"{self.cardinality_limit}. A label is probably unbounded — a patient id, "
                    f"a query, or a correlation id. Labels: {sorted(labels)}"
                )
        return key


class MetricRegistry:
    """Holds every metric and renders the Prometheus exposition format.

    Thread-safe: metrics are written from request handlers, background workers, and the event
    bus, and a partially-updated histogram read by a scrape is a corrupt data point rather
    than a slightly stale one.
    """

    def __init__(self, *, cardinality_limit: int = DEFAULT_CARDINALITY_LIMIT) -> None:
        self._series: dict[str, _Series] = {}
        self._lock = threading.Lock()
        self._cardinality_limit = cardinality_limit

    def counter(self, name: str, help_text: str, labels: tuple[str, ...] = ()) -> Counter:
        return Counter(self._declare(name, "counter", help_text, labels), self)

    def gauge(self, name: str, help_text: str, labels: tuple[str, ...] = ()) -> Gauge:
        return Gauge(self._declare(name, "gauge", help_text, labels), self)

    def histogram(
        self,
        name: str,
        help_text: str,
        labels: tuple[str, ...] = (),
        buckets: tuple[float, ...] = DEFAULT_BUCKETS,
    ) -> Histogram:
        series = self._declare(name, "histogram", help_text, labels)
        series.buckets = buckets
        return Histogram(series, self)

    def _declare(self, name: str, kind: str, help_text: str, labels: tuple[str, ...]) -> _Series:
        with self._lock:
            existing = self._series.get(name)
            if existing is not None:
                if existing.kind != kind or set(existing.label_names) != set(labels):
                    # Redeclaring a metric with a different shape produces two incompatible
                    # series under one name, and every query against it silently mixes them.
                    raise MetricsError(
                        f"Metric '{name}' is already declared as {existing.kind} with labels "
                        f"{sorted(existing.label_names)}"
                    )
                return existing
            series = _Series(
                name=name,
                kind=kind,
                help_text=help_text,
                label_names=tuple(labels),
                cardinality_limit=self._cardinality_limit,
            )
            self._series[name] = series
            return series

    def _add(self, series: _Series, labels: dict[str, str], amount: float) -> None:
        key = series.check(labels)
        with self._lock:
            series.values[key] = series.values.get(key, 0.0) + amount

    def _set(self, series: _Series, labels: dict[str, str], value: float) -> None:
        key = series.check(labels)
        with self._lock:
            series.values[key] = value

    def _observe(self, series: _Series, labels: dict[str, str], value: float) -> None:
        if math.isnan(value) or math.isinf(value):
            # NaN in a histogram poisons every quantile computed from it, and the resulting
            # dashboard reads as "no data" rather than "bad data".
            raise MetricsError(f"Metric '{series.name}' cannot observe {value}")
        key = series.check(labels)
        with self._lock:
            counts = series.bucket_counts.setdefault(key, [0] * len(series.buckets))
            for index, edge in enumerate(series.buckets):
                if value <= edge:
                    counts[index] += 1
            series.sums[key] = series.sums.get(key, 0.0) + value
            series.counts[key] = series.counts.get(key, 0) + 1

    def render(self) -> str:
        """Prometheus text exposition format."""
        lines: list[str] = []
        with self._lock:
            for name in sorted(self._series):
                series = self._series[name]
                lines.append(f"# HELP {name} {series.help_text}")
                lines.append(f"# TYPE {name} {series.kind}")
                if series.kind == "histogram":
                    for key in sorted(series.counts):
                        labels = dict(key)
                        cumulative = series.bucket_counts.get(key, [])
                        for edge, count in zip(series.buckets, cumulative, strict=True):
                            lines.append(
                                f"{name}_bucket{_render_labels({**labels, 'le': _fmt(edge)})} "
                                f"{count}"
                            )
                        total = series.counts[key]
                        lines.append(
                            f"{name}_bucket{_render_labels({**labels, 'le': '+Inf'})} {total}"
                        )
                        lines.append(f"{name}_sum{_render_labels(labels)} {series.sums[key]}")
                        lines.append(f"{name}_count{_render_labels(labels)} {total}")
                else:
                    for key in sorted(series.values):
                        lines.append(f"{name}{_render_labels(dict(key))} {series.values[key]}")
        return "\n".join(lines) + "\n"

    def collect(self) -> dict[str, float]:
        """Flat name→value snapshot, for tests and the health endpoint."""
        snapshot: dict[str, float] = {}
        with self._lock:
            for name, series in self._series.items():
                if series.kind == "histogram":
                    for key, count in series.counts.items():
                        snapshot[f"{name}_count{_render_labels(dict(key))}"] = float(count)
                        snapshot[f"{name}_sum{_render_labels(dict(key))}"] = series.sums[key]
                else:
                    for key, value in series.values.items():
                        snapshot[f"{name}{_render_labels(dict(key))}"] = value
        return snapshot

    def series_names(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._series))


def _fmt(value: float) -> str:
    return repr(value) if value != int(value) else str(int(value))


def _render_labels(labels: dict[str, str]) -> str:
    if not labels:
        return ""
    inner = ",".join(f'{k}="{_escape(str(v))}"' for k, v in sorted(labels.items()))
    return "{" + inner + "}"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


@dataclass(frozen=True, slots=True)
class Counter:
    """Monotonically increasing count."""

    _series: _Series
    _registry: MetricRegistry

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        if amount < 0:
            raise MetricsError("A counter cannot decrease")
        self._registry._add(self._series, labels, amount)


@dataclass(frozen=True, slots=True)
class Gauge:
    """A value that goes up and down."""

    _series: _Series
    _registry: MetricRegistry

    def set(self, value: float, **labels: str) -> None:
        self._registry._set(self._series, labels, value)


@dataclass(frozen=True, slots=True)
class Histogram:
    """Distribution of observations."""

    _series: _Series
    _registry: MetricRegistry

    def observe(self, value: float, **labels: str) -> None:
        self._registry._observe(self._series, labels, value)
