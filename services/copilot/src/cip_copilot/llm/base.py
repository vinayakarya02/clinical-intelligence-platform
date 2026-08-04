"""The language-model seam.

One protocol, two methods, and token usage on every call. Deliberately narrow: the model's
job in this system is language, not control flow (ADR-0009), so there is no `decide()`, no
tool-choice hook, and no agent loop to plug into.

``extract`` is separate from ``complete`` because the two have different failure modes and
different verification. Extraction pulls a value *out of* text that is present, and a wrong
answer is checkable against the source. Completion composes prose, and its output is checked
claim-by-claim afterwards. Collapsing them into one call would lose that distinction and the
ability to hold each to its own standard.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from cip_copilot.domain import TokenUsage

__all__ = [
    "GenerationRequest",
    "LanguageModel",
    "LanguageModelError",
    "LanguageModelInfo",
    "ModelResponse",
]


class LanguageModelError(RuntimeError):
    """A model call failed.

    ``retryable`` distinguishes a transient transport failure from a refusal or a malformed
    request; retrying the latter burns budget and delays the real error.
    """

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class LanguageModelInfo:
    """Identity of the model behind a response."""

    provider: str
    model_name: str
    max_context_tokens: int = 8192
    supports_structured_output: bool = False

    @property
    def key(self) -> str:
        """Recorded on every answer, so a quality change can be attributed to a model change."""
        return f"{self.provider}/{self.model_name}"


@dataclass(frozen=True, slots=True)
class GenerationRequest:
    """One completion request.

    ``max_output_tokens`` is required rather than optional: an unbounded generation against a
    metered provider is a cost incident waiting to happen, and a caller that has not thought
    about output length has not thought about the prompt either.
    """

    system: str
    user: str
    max_output_tokens: int = 800
    temperature: float = 0.0
    """Zero by default. A clinical answer that varies between identical runs cannot be
    reproduced during review, and reproducibility is worth more here than variety."""
    json_schema: dict[str, Any] | None = None
    """When set, the response must conform. Providers that support constrained decoding
    enforce it; implementations that cannot must validate and raise rather than return
    something that merely looks close."""

    def __post_init__(self) -> None:
        if self.max_output_tokens < 1:
            raise ValueError("max_output_tokens must be >= 1")
        if not 0.0 <= self.temperature <= 2.0:
            raise ValueError("temperature must be in [0, 2]")


@dataclass(frozen=True, slots=True)
class ModelResponse:
    """What a model returned, with what it cost."""

    text: str
    usage: TokenUsage = field(default_factory=TokenUsage)
    model_key: str = ""
    finish_reason: str = "stop"
    structured: dict[str, Any] | None = None

    @property
    def was_truncated(self) -> bool:
        """A truncated clinical answer can end mid-caveat, which reads as a complete one."""
        return self.finish_reason == "length"


@runtime_checkable
class LanguageModel(Protocol):
    """Generates and extracts natural language."""

    @property
    def info(self) -> LanguageModelInfo: ...

    async def complete(self, request: GenerationRequest) -> ModelResponse:
        """Compose prose from a system and user prompt."""
        ...

    async def extract(self, *, text: str, target: str) -> str | None:
        """Pull ``target`` out of ``text``, or ``None`` if it is not present.

        Returning ``None`` rather than a best guess is the contract that matters: a caller
        asking for a potassium value needs to know the difference between "5.4" and "not
        stated", and a plausible invention is worse than an absence.
        """
        ...
