"""Prompt registry.

Prompts are versioned assets, not string literals. They are edited by people who are not
changing code (clinical safety reviewers, prompt engineers), they need review and rollback
independently of a deploy, and an answer's behaviour must be traceable to the exact prompt
version that produced it. A prompt embedded in a function satisfies none of that.

So templates live in ``templates/*.yaml``, are loaded by name and version, and every render
records which version was used. The registry validates on load: an undeclared placeholder
is a rendering failure at request time, which is the worst moment to discover it, so the
check happens at startup instead.

Layering follows the Phase 0 design (docs/architecture/04-conversational-ai.md): a system
prompt establishes role and safety boundaries, a developer prompt carries task-invariant
platform rules, a task prompt states the immediate job, and grounding/citation fragments
enforce the evidence discipline. They compose rather than nest, so a change to citation
rules applies everywhere without editing each task prompt.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from cip_core.errors import CipError
from cip_core.logging import get_logger
from cip_ingestion.processing.tokenization import HeuristicTokenEstimator

__all__ = ["PromptNotFoundError", "PromptRegistry", "PromptTemplate", "RenderedPrompt"]

_log = get_logger(__name__)

_PLACEHOLDER = re.compile(r"\{\{\s*(\w+)\s*\}\}")

_TEMPLATE_ROOT = Path(__file__).parent / "templates"

#: Version strings must be zero-padded so lexicographic ordering matches numeric
#: ordering. `get()` selects the newest version with `max()` over strings, so an
#: unpadded "v9" would sort *above* "v10" and silently serve a superseded prompt —
#: a failure that shows up as an unexplained answer-quality regression, not an error.
_VERSION_FORMAT = re.compile(r"^v\d{3,}$")


class PromptNotFoundError(CipError):
    """Requested prompt name or version does not exist."""

    status = 500
    problem_type = "prompt-not-found"
    title = "Prompt template not found"


class PromptRenderError(CipError):
    """A required placeholder had no value."""

    status = 500
    problem_type = "prompt-render-failed"
    title = "Prompt rendering failed"


@dataclass(frozen=True, slots=True)
class PromptTemplate:
    """One versioned prompt fragment."""

    name: str
    version: str
    role: str
    """``system`` | ``developer`` | ``task`` | ``fragment``."""
    body: str
    description: str = ""
    required_variables: frozenset[str] = field(default_factory=frozenset)

    @property
    def key(self) -> str:
        return f"{self.name}@{self.version}"

    def placeholders(self) -> frozenset[str]:
        return frozenset(_PLACEHOLDER.findall(self.body))

    def render(self, variables: dict[str, Any]) -> str:
        """Substitute placeholders.

        Missing *required* variables raise rather than rendering an empty string: a
        grounding prompt silently missing its evidence block would produce a fluent,
        confident, entirely ungrounded answer.
        """
        missing = self.required_variables - variables.keys()
        if missing:
            raise PromptRenderError(
                f"Prompt '{self.key}' is missing required variables: {sorted(missing)}"
            )

        def _substitute(match: re.Match[str]) -> str:
            return str(variables.get(match.group(1), ""))

        return _PLACEHOLDER.sub(_substitute, self.body).strip()


@dataclass(frozen=True, slots=True)
class RenderedPrompt:
    """A composed prompt plus the provenance of what produced it."""

    system: str
    user: str
    template_versions: dict[str, str]
    """Name to version for every template used. Recorded on the response so an answer can
    be traced to the exact prompts that produced it."""
    estimated_tokens: int = 0

    def as_messages(self) -> list[dict[str, str]]:
        """Render as a provider-agnostic message list."""
        return [
            {"role": "system", "content": self.system},
            {"role": "user", "content": self.user},
        ]


class PromptRegistry:
    """Loads, validates, and renders versioned prompt templates."""

    def __init__(self, root: Path | None = None) -> None:
        self._root = root or _TEMPLATE_ROOT
        self._templates: dict[str, dict[str, PromptTemplate]] = {}
        self._tokens = HeuristicTokenEstimator()
        self._load()

    def _load(self) -> None:
        """Load every template file and validate placeholder declarations."""
        if not self._root.is_dir():
            raise PromptNotFoundError(f"Prompt template directory not found: {self._root}")

        for path in sorted(self._root.glob("*.yaml")):
            payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            for entry in payload.get("prompts", []):
                template = PromptTemplate(
                    name=entry["name"],
                    version=str(entry["version"]),
                    role=entry.get("role", "fragment"),
                    body=entry["body"],
                    description=entry.get("description", ""),
                    required_variables=frozenset(entry.get("required_variables", [])),
                )
                # A required variable that appears nowhere in the body is a template bug —
                # usually a rename that missed one side — and would otherwise surface as a
                # confusing runtime error about a variable the prompt never uses.
                undeclared = template.required_variables - template.placeholders()
                if undeclared:
                    raise PromptNotFoundError(
                        f"Prompt '{template.key}' requires variables that do not appear in "
                        f"its body: {sorted(undeclared)}"
                    )
                if not _VERSION_FORMAT.match(template.version):
                    raise PromptNotFoundError(
                        f"Prompt '{template.name}' has version '{template.version}', which is "
                        "not zero-padded (expected v001, v002, ...). Version selection is a "
                        "lexicographic max, so an unpadded version silently mis-orders."
                    )
                self._templates.setdefault(template.name, {})[template.version] = template

        if not self._templates:
            raise PromptNotFoundError(f"No prompt templates found in {self._root}")

        _log.info(
            "prompts.loaded",
            names=len(self._templates),
            total_versions=sum(len(v) for v in self._templates.values()),
        )

    def get(self, name: str, *, version: str | None = None) -> PromptTemplate:
        """Fetch a template. ``version=None`` selects the highest version.

        "Highest" is a lexicographic max over version strings, which is why versions are
        zero-padded (``v001``) in the shipped templates — ``v10`` must not sort below
        ``v9``.
        """
        versions = self._templates.get(name)
        if not versions:
            raise PromptNotFoundError(f"No prompt registered under name '{name}'")
        if version is None:
            version = max(versions)
        template = versions.get(version)
        if template is None:
            raise PromptNotFoundError(f"Prompt '{name}' has no version '{version}'")
        return template

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._templates))

    def versions(self, name: str) -> tuple[str, ...]:
        return tuple(sorted(self._templates.get(name, {})))

    def compose(
        self,
        *,
        system: str = "clinical_system",
        developer: str = "clinical_developer",
        task: str,
        variables: dict[str, Any],
        versions: dict[str, str] | None = None,
    ) -> RenderedPrompt:
        """Compose system + developer + task into a single prompt.

        System and developer fragments are concatenated into the system message because
        they express the same thing to the model — standing rules — while the task and its
        evidence form the user message.
        """
        versions = versions or {}
        used: dict[str, str] = {}

        parts: list[str] = []
        for name in (system, developer):
            template = self.get(name, version=versions.get(name))
            used[template.name] = template.version
            parts.append(template.render(variables))

        task_template = self.get(task, version=versions.get(task))
        used[task_template.name] = task_template.version

        rendered = RenderedPrompt(
            system="\n\n".join(part for part in parts if part),
            user=task_template.render(variables),
            template_versions=used,
        )

        from cip_ingestion.processing.tokenization import HeuristicTokenEstimator

        estimator = HeuristicTokenEstimator()
        return RenderedPrompt(
            system=rendered.system,
            user=rendered.user,
            template_versions=used,
            estimated_tokens=estimator.count(rendered.system) + estimator.count(rendered.user),
        )
