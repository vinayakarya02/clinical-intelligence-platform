"""Prompt registry v2 — deployment pins, rollback, and experiments.

Phase 2's :class:`~cip_retrieval.prompts.registry.PromptRegistry` loads versioned templates
and validates them. That is the storage layer, and it is unchanged. This adds the operational
layer on top: which version is *live*, how to roll back without a deploy, and how to run an
experiment.

Three properties matter in a clinical setting, and each is the reason for one design choice.

**Rollback must not require a code change.** A prompt that turns out to phrase a caveat badly
is a clinical-safety issue with a deploy-cycle-long fix if the version is pinned in code. A
:class:`Deployment` is data, so reverting is a configuration change.

**An experiment must be deterministic per session.** Bucketing on a hash of the session id
means one conversation never switches prompt versions mid-way — a clinician watching the
assistant change its phrasing between turns has no way to tell an experiment from a
malfunction.

**Every answer records the exact versions that produced it.** Without that, an answer-quality
regression cannot be attributed to a prompt change, which is the whole point of versioning.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from cip_core.errors import CipError
from cip_core.logging import get_logger
from cip_retrieval import prompts as cip_retrieval_prompts
from cip_retrieval.prompts.registry import PromptRegistry, PromptTemplate

__all__ = ["Deployment", "Experiment", "PromptCatalog", "PromptSelection"]

_log = get_logger(__name__)


class PromptConfigurationError(CipError):
    """A deployment or experiment names something that does not exist."""

    status = 500
    problem_type = "prompt-configuration-invalid"
    title = "Prompt configuration is invalid"


@dataclass(frozen=True, slots=True)
class Experiment:
    """An A/B split between two versions of one prompt."""

    name: str
    prompt_name: str
    control_version: str
    variant_version: str
    variant_share: float = 0.5

    def __post_init__(self) -> None:
        if not 0.0 < self.variant_share < 1.0:
            raise ValueError("variant_share must be strictly between 0 and 1")
        if self.control_version == self.variant_version:
            raise ValueError("An experiment needs two different versions")

    def assign(self, session_id: str) -> str:
        """Which version this session gets. Stable for the life of the session."""
        digest = hashlib.sha256(f"{self.name}:{session_id}".encode()).digest()
        bucket = int.from_bytes(digest[:4], "big") / 0xFFFFFFFF
        return self.variant_version if bucket < self.variant_share else self.control_version


@dataclass(frozen=True, slots=True)
class Deployment:
    """Which prompt version is live, per prompt name."""

    pins: dict[str, str] = field(default_factory=dict)
    """Explicit name → version pins. A name absent here uses the newest version."""

    experiments: tuple[Experiment, ...] = ()

    def with_pin(self, prompt_name: str, version: str) -> Deployment:
        """Return a deployment with one prompt pinned — the rollback operation."""
        return Deployment(pins={**self.pins, prompt_name: version}, experiments=self.experiments)


@dataclass(frozen=True, slots=True)
class PromptSelection:
    """A resolved template and why that version was chosen."""

    template: PromptTemplate
    reason: str

    @property
    def version(self) -> str:
        return self.template.version


class PromptCatalog:
    """Resolves which prompt version serves a given session."""

    #: Template roots, searched in order. The retrieval service owns the standing safety
    #: prompts and the copilot owns its task prompts; both are loaded so a task prompt is a
    #: versioned, roll-back-able asset like every other, rather than an f-string in a stage.
    DEFAULT_ROOTS: tuple[Path, ...] = (
        Path(cip_retrieval_prompts.__file__).parent / "templates",
        Path(__file__).parent / "templates",
    )

    def __init__(
        self,
        registry: PromptRegistry | None = None,
        *,
        deployment: Deployment | None = None,
        root: Path | None = None,
        roots: tuple[Path, ...] | None = None,
    ) -> None:
        if registry is not None:
            self._registries = [registry]
        else:
            chosen = (root,) if root is not None else (roots or self.DEFAULT_ROOTS)
            self._registries = [PromptRegistry(root=r) for r in chosen]
        self._deployment = deployment or Deployment()
        self._validate()

    def _lookup(self, name: str, version: str | None = None) -> PromptTemplate:
        """Find a template across every configured root.

        First root wins on a name collision, and the order is fixed rather than merged, so
        which template serves a name is a property of configuration rather than of
        filesystem iteration order.
        """
        last: Exception | None = None
        for registry in self._registries:
            try:
                return registry.get(name, version=version)
            except Exception as exc:
                last = exc
        raise PromptConfigurationError(f"No prompt '{name}' in any configured root") from last

    def _validate(self) -> None:
        """Reject a deployment naming a prompt or version that does not exist.

        At construction, not at request time: a pin pointing at a deleted version is a
        configuration error, and discovering it when a clinician asks a question is the worst
        available moment.
        """
        for name, version in self._deployment.pins.items():
            try:
                self._lookup(name, version=version)
            except Exception as exc:
                raise PromptConfigurationError(
                    f"Deployment pins '{name}' to version '{version}', which does not exist"
                ) from exc

        for experiment in self._deployment.experiments:
            for version in (experiment.control_version, experiment.variant_version):
                try:
                    self._lookup(experiment.prompt_name, version=version)
                except Exception as exc:
                    raise PromptConfigurationError(
                        f"Experiment '{experiment.name}' names version '{version}' of "
                        f"'{experiment.prompt_name}', which does not exist"
                    ) from exc

    @property
    def deployment(self) -> Deployment:
        return self._deployment

    def with_deployment(self, deployment: Deployment) -> PromptCatalog:
        """A catalog serving a different deployment. Used to roll back or shift an
        experiment."""
        catalog = PromptCatalog.__new__(PromptCatalog)
        catalog._registries = self._registries
        catalog._deployment = deployment
        catalog._validate()
        return catalog

    def select(self, prompt_name: str, *, session_id: str = "") -> PromptSelection:
        """Resolve the version for this session.

        Precedence is pin, then experiment, then newest. A pin wins over an experiment because
        a pin is how a live problem gets rolled back, and an experiment must never be able to
        keep serving a version that was just pulled.
        """
        pinned = self._deployment.pins.get(prompt_name)
        if pinned is not None:
            return PromptSelection(
                template=self._lookup(prompt_name, version=pinned),
                reason=f"pinned to {pinned}",
            )

        for experiment in self._deployment.experiments:
            if experiment.prompt_name != prompt_name:
                continue
            version = experiment.assign(session_id)
            arm = "variant" if version == experiment.variant_version else "control"
            return PromptSelection(
                template=self._lookup(prompt_name, version=version),
                reason=f"experiment {experiment.name}:{arm}",
            )

        template = self._lookup(prompt_name)
        return PromptSelection(template=template, reason="newest")

    def render(
        self, prompt_name: str, variables: dict[str, object], *, session_id: str = ""
    ) -> tuple[str, str]:
        """Render a prompt, returning ``(text, version)``."""
        selection = self.select(prompt_name, session_id=session_id)
        return selection.template.render(dict(variables)), selection.version

    def compose_system(self, *, session_id: str = "") -> tuple[str, dict[str, str]]:
        """The layered system prompt, with the versions that produced it.

        System and developer layers are concatenated because they say the same kind of thing
        to a model — standing rules that a task prompt cannot relax.
        """
        parts: list[str] = []
        versions: dict[str, str] = {}
        for name in ("clinical_system", "clinical_developer"):
            selection = self.select(name, session_id=session_id)
            rendered = selection.template.render({})
            if rendered:
                parts.append(rendered)
            versions[name] = selection.version
            _log.debug(
                "prompts.selected", prompt=name, version=selection.version, reason=selection.reason
            )
        return "\n\n".join(parts), versions

    def compose_task(
        self, name: str, variables: dict[str, object], *, session_id: str = ""
    ) -> tuple[str, str]:
        """Render a task prompt and report the version that served it.

        Stages must use this rather than building a prompt inline. An f-string in a stage is
        unversioned, un-rollback-able, and — because claim text contains verbatim passages
        from user-uploaded documents — carries no injection boundary.
        """
        selection = self.select(name, session_id=session_id)
        return selection.template.render(dict(variables)), selection.version

    def names(self) -> tuple[str, ...]:
        seen: list[str] = []
        for registry in self._registries:
            seen.extend(n for n in registry.names() if n not in seen)
        return tuple(sorted(seen))

    def versions(self, prompt_name: str) -> tuple[str, ...]:
        for registry in self._registries:
            found = registry.versions(prompt_name)
            if found:
                return found
        return ()
