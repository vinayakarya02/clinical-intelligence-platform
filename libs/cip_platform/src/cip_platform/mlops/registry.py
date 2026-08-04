"""Model, embedding, and evaluation registries, and the compatibility matrix.

One idea runs through all of them: **an artifact version is data, and promotion is a
transition that can be reversed.** A model pinned in code needs a deploy to roll back, and a
deploy cycle is the wrong unit of time for "the new model is answering badly".

The compatibility matrix is the part that does real work. A deployment names a model version,
a prompt version, and an embedding version, and the matrix says whether that *combination* has
been evaluated. Each component being individually blessed says nothing about the three
together — an embedding change alters what is retrieved, which changes what the prompt is
given, which changes what the model answers. An un-evaluated combination is refusable at
startup rather than discoverable in production.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

from cip_core.errors import CipError
from cip_core.logging import get_logger

__all__ = [
    "ArtifactKind",
    "CompatibilityMatrix",
    "DeploymentSet",
    "EvaluationRecord",
    "ModelRegistry",
    "ModelVersion",
    "RegistryError",
    "Stage",
]

_log = get_logger(__name__)


class RegistryError(CipError):
    """An invalid registry operation."""

    status = 409
    problem_type = "registry-conflict"
    title = "Registry operation refused"


class Stage(StrEnum):
    """Where a version sits in its lifecycle."""

    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"
    ARCHIVED = "archived"


class ArtifactKind(StrEnum):
    """What kind of versioned thing this is."""

    LANGUAGE_MODEL = "language_model"
    EMBEDDING_MODEL = "embedding_model"
    RERANKER = "reranker"
    PROMPT = "prompt"


@dataclass(frozen=True, slots=True)
class ModelVersion:
    """One version of one artifact."""

    name: str
    version: str
    kind: ArtifactKind
    stage: Stage = Stage.DEVELOPMENT
    uri: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    registered_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))
    promoted_at: dt.datetime | None = None

    @property
    def key(self) -> str:
        return f"{self.name}@{self.version}"

    @property
    def dimensions(self) -> int | None:
        """Embedding width, when this is an embedding model.

        Surfaced as a first-class property because a dimension change is the one model change
        that makes an existing vector index unreadable rather than merely worse.
        """
        raw = self.metadata.get("dimensions")
        return int(raw) if raw is not None else None


class ModelRegistry:
    """Versions, stages, promotion, and rollback.

    At most one version per (name, kind) may be in ``PRODUCTION``. Promotion demotes the
    incumbent and records it, which is what makes rollback a single call rather than an
    archaeology exercise.
    """

    def __init__(self) -> None:
        self._versions: dict[tuple[str, str], ModelVersion] = {}
        self._previous_production: dict[str, str] = {}

    def register(self, version: ModelVersion) -> ModelVersion:
        key = (version.name, version.version)
        if key in self._versions:
            raise RegistryError(f"{version.key} is already registered")
        if version.stage is Stage.PRODUCTION:
            # Registering straight into production skips the demotion bookkeeping that makes
            # rollback possible, so it is refused: register, then promote.
            raise RegistryError(
                f"{version.key} cannot be registered directly into production; "
                "register it first and promote it"
            )
        self._versions[key] = version
        _log.info("registry.registered", artifact=version.key, kind=str(version.kind))
        return version

    def get(self, name: str, version: str) -> ModelVersion:
        found = self._versions.get((name, version))
        if found is None:
            raise RegistryError(f"No version {name}@{version} is registered")
        return found

    def production(self, name: str) -> ModelVersion | None:
        """The version currently serving, if any."""
        for version in self._versions.values():
            if version.name == name and version.stage is Stage.PRODUCTION:
                return version
        return None

    def promote(self, name: str, version: str, *, to: Stage) -> ModelVersion:
        """Move a version to ``to``, demoting any incumbent."""
        candidate = self.get(name, version)
        if candidate.stage is Stage.ARCHIVED:
            raise RegistryError(f"{candidate.key} is archived and cannot be promoted")

        if to is Stage.PRODUCTION:
            incumbent = self.production(name)
            if incumbent is not None:
                if incumbent.version == version:
                    return incumbent
                self._versions[(name, incumbent.version)] = replace(incumbent, stage=Stage.STAGING)
                # Remembered so rollback does not have to guess which version was serving.
                self._previous_production[name] = incumbent.version
                _log.info("registry.demoted", artifact=incumbent.key, to=str(Stage.STAGING))

        promoted = replace(candidate, stage=to, promoted_at=dt.datetime.now(dt.UTC))
        self._versions[(name, version)] = promoted
        _log.info("registry.promoted", artifact=promoted.key, stage=str(to))
        return promoted

    def rollback(self, name: str) -> ModelVersion:
        """Return the previous production version to production.

        One call, no deploy. That is the whole point: the interval between noticing a bad
        model and reverting it should be seconds, not a release cycle.
        """
        previous = self._previous_production.get(name)
        if previous is None:
            raise RegistryError(f"No previous production version recorded for '{name}'")
        current = self.production(name)
        rolled = self.promote(name, previous, to=Stage.PRODUCTION)
        if current is not None:
            self._versions[(name, current.version)] = replace(
                self._versions[(name, current.version)], stage=Stage.ARCHIVED
            )
            _log.warning("registry.rolled_back", from_version=current.key, to_version=rolled.key)
        return rolled

    def versions(self, name: str | None = None) -> tuple[ModelVersion, ...]:
        found = [v for v in self._versions.values() if name is None or v.name == name]
        return tuple(sorted(found, key=lambda v: (v.name, v.version)))


@dataclass(frozen=True, slots=True)
class EvaluationRecord:
    """One scored evaluation run, tied to what produced it.

    The artifact versions are part of the record rather than a note on it. An evaluation
    score that does not say which model, prompt, and embedding produced it cannot be compared
    against another one, which makes it decoration.
    """

    run_id: str
    model_version: str
    prompt_version: str
    embedding_version: str
    metrics: dict[str, float]
    dataset: str = ""
    recorded_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))
    tenant_id: uuid.UUID | None = None

    @property
    def combination(self) -> tuple[str, str, str]:
        return (self.model_version, self.prompt_version, self.embedding_version)

    def meets(self, thresholds: dict[str, float]) -> bool:
        """Whether every named threshold is satisfied.

        A missing metric fails. Treating absence as success is how an evaluation gate stops
        gating: a renamed metric would silently pass everything.
        """
        return all(self.metrics.get(name, -1.0) >= floor for name, floor in thresholds.items())


@dataclass(frozen=True, slots=True)
class DeploymentSet:
    """The combination of artifact versions a deployment runs."""

    model_version: str
    prompt_version: str
    embedding_version: str

    @property
    def combination(self) -> tuple[str, str, str]:
        return (self.model_version, self.prompt_version, self.embedding_version)

    def describe(self) -> str:
        return (
            f"model={self.model_version} prompt={self.prompt_version} "
            f"embedding={self.embedding_version}"
        )


class CompatibilityMatrix:
    """Which artifact combinations have been evaluated, and whether they passed.

    Consulted at startup. An un-evaluated combination is a deployment nobody tested, and
    refusing to start is a controlled failure where serving it is an uncontrolled one.
    """

    def __init__(self, *, thresholds: dict[str, float] | None = None) -> None:
        self._records: dict[tuple[str, str, str], EvaluationRecord] = {}
        self._thresholds = thresholds or {}

    def record(self, evaluation: EvaluationRecord) -> None:
        """Store a run. A newer run for the same combination replaces the older one."""
        existing = self._records.get(evaluation.combination)
        if existing is not None and existing.recorded_at > evaluation.recorded_at:
            return
        self._records[evaluation.combination] = evaluation

    def evaluation_for(self, deployment: DeploymentSet) -> EvaluationRecord | None:
        return self._records.get(deployment.combination)

    def is_supported(self, deployment: DeploymentSet) -> bool:
        record = self.evaluation_for(deployment)
        return record is not None and record.meets(self._thresholds)

    def require_supported(self, deployment: DeploymentSet) -> EvaluationRecord:
        """Raise unless this combination has been evaluated and passed."""
        record = self.evaluation_for(deployment)
        if record is None:
            raise RegistryError(
                f"No evaluation exists for {deployment.describe()}. Every component may be "
                "individually approved and the combination still untested — an embedding "
                "change alters what is retrieved, which changes what the model is given."
            )
        if not record.meets(self._thresholds):
            failed = {
                name: record.metrics.get(name, -1.0)
                for name, floor in self._thresholds.items()
                if record.metrics.get(name, -1.0) < floor
            }
            raise RegistryError(
                f"{deployment.describe()} was evaluated but did not meet thresholds: {failed}"
            )
        return record

    def supported_combinations(self) -> tuple[tuple[str, str, str], ...]:
        return tuple(sorted(c for c, r in self._records.items() if r.meets(self._thresholds)))
