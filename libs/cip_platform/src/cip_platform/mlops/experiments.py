"""Experiment tracking, behind a protocol.

MLflow is the intended backend and is not a dependency of this library. The protocol is
narrow — start a run, log parameters, log metrics, log an artifact reference, end the run —
because that is the intersection of what every tracking backend offers and what this platform
needs. A wider interface would bind us to one vendor's semantics for the sake of features
nobody here uses.

The in-memory implementation is not a mock: it records the same data and is what the
evaluation harness writes to in tests and in local runs.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from cip_core.logging import get_logger

__all__ = ["ExperimentRun", "ExperimentTracker", "InMemoryExperimentTracker"]

_log = get_logger(__name__)


@dataclass(slots=True)
class ExperimentRun:
    """One tracked run."""

    run_id: str
    experiment: str
    params: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, float] = field(default_factory=dict)
    artifacts: dict[str, str] = field(default_factory=dict)
    started_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))
    ended_at: dt.datetime | None = None
    status: str = "running"

    @property
    def duration_seconds(self) -> float | None:
        if self.ended_at is None:
            return None
        return (self.ended_at - self.started_at).total_seconds()


@runtime_checkable
class ExperimentTracker(Protocol):
    """Records runs, parameters, and metrics."""

    def start_run(self, experiment: str, *, params: dict[str, Any] | None = None) -> str: ...

    def log_metrics(self, run_id: str, metrics: dict[str, float]) -> None: ...

    def log_artifact(self, run_id: str, name: str, uri: str) -> None: ...

    def end_run(self, run_id: str, *, status: str = "finished") -> ExperimentRun: ...


class InMemoryExperimentTracker:
    """In-process tracking with the same record shape a backend would store."""

    def __init__(self) -> None:
        self._runs: dict[str, ExperimentRun] = {}

    def start_run(self, experiment: str, *, params: dict[str, Any] | None = None) -> str:
        run_id = uuid.uuid4().hex
        self._runs[run_id] = ExperimentRun(
            run_id=run_id, experiment=experiment, params=dict(params or {})
        )
        return run_id

    def log_metrics(self, run_id: str, metrics: dict[str, float]) -> None:
        run = self._require(run_id)
        # Merge rather than replace: metrics arrive from several stages of one evaluation,
        # and the last writer replacing the set would silently discard the earlier ones.
        run.metrics.update(metrics)

    def log_artifact(self, run_id: str, name: str, uri: str) -> None:
        self._require(run_id).artifacts[name] = uri

    def end_run(self, run_id: str, *, status: str = "finished") -> ExperimentRun:
        run = self._require(run_id)
        run.ended_at = dt.datetime.now(dt.UTC)
        run.status = status
        _log.info(
            "experiment.finished",
            experiment=run.experiment,
            status=status,
            metric_count=len(run.metrics),
        )
        return run

    def get(self, run_id: str) -> ExperimentRun | None:
        return self._runs.get(run_id)

    def runs(self, experiment: str | None = None) -> tuple[ExperimentRun, ...]:
        found = [r for r in self._runs.values() if experiment is None or r.experiment == experiment]
        return tuple(sorted(found, key=lambda r: r.started_at))

    def _require(self, run_id: str) -> ExperimentRun:
        run = self._runs.get(run_id)
        if run is None:
            raise KeyError(f"No such run '{run_id}'")
        return run
