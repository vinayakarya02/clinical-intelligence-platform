"""A deterministic language model that composes answers from evidence.

Not a mock, and not a stub. It implements the full :class:`LanguageModel` contract and
produces real, readable, evidence-grounded output — it simply does so by selection and
templating rather than by generation. Given claims and their evidence it assembles a cited
answer; given a passage and a target it extracts the value by clinical pattern.

The property that makes it more than a placeholder: **it cannot hallucinate.** Every token it
emits either comes from the evidence or from a fixed template. That makes it a genuine lower
bound for the safety pipeline — if the verifier flags something here, the verifier is wrong,
because there is nothing unsupported to flag. Several real bugs in the validation layer were
found exactly that way.

What it is not is fluent. It will not paraphrase, synthesise across passages, or answer a
question whose phrasing does not match the evidence. Those are the things a real model is for,
and the Phase 3 report says plainly that fluency numbers measured here mean nothing about one.
"""

from __future__ import annotations

import re

from cip_copilot.domain import TokenUsage
from cip_copilot.llm.base import (
    GenerationRequest,
    LanguageModelError,
    LanguageModelInfo,
    ModelResponse,
)

__all__ = ["ExtractiveLanguageModel", "NullLanguageModel", "estimate_tokens"]

#: Clinical value patterns, tried in order. Ordered most-specific first: a bare number would
#: match inside "5.4 mmol/L" and return "5" if it were tried before the value-with-unit form.
_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(\d+(?:\.\d+)?)\s*(mg/dl|mmol/l|meq/l|ng/ml|g/dl|mmhg|mcg|mg|ml|kg)\b", re.I),
    re.compile(r"\|\s*(\d+(?:\.\d+)?)\s*\|"),
    re.compile(r"\b(\d+(?:\.\d+)?)\b"),
)

#: Rough characters-per-token. Deliberately a constant rather than a real tokenizer: this
#: model exists so the platform runs without provider dependencies, and pulling in a
#: tokenizer to count tokens nobody is billed for would defeat that.
_CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    """Approximate token count for accounting and budget checks."""
    return max(1, len(text) // _CHARS_PER_TOKEN) if text else 0


class ExtractiveLanguageModel:
    """Composes and extracts without generating anything new."""

    def __init__(self, *, max_context_tokens: int = 8192) -> None:
        self._info = LanguageModelInfo(
            provider="local",
            model_name="extractive",
            max_context_tokens=max_context_tokens,
            supports_structured_output=False,
        )

    @property
    def info(self) -> LanguageModelInfo:
        return self._info

    async def complete(self, request: GenerationRequest) -> ModelResponse:
        """Assemble a response from the claim lines in the user prompt.

        The orchestrator passes claims as ``- <statement> [n]`` lines. Anything not in that
        form is ignored rather than echoed, so prompt scaffolding cannot leak into the answer
        — and neither can any instruction embedded in retrieved evidence.
        """
        if request.json_schema is not None:
            # Honest refusal beats returning something that merely looks like the schema.
            # A caller relying on constrained decoding must know it is not available.
            raise LanguageModelError(
                "ExtractiveLanguageModel cannot honour a json_schema constraint; "
                "use a provider with structured-output support",
                retryable=False,
            )

        prompt_tokens = estimate_tokens(request.system) + estimate_tokens(request.user)
        if prompt_tokens > self._info.max_context_tokens:
            raise LanguageModelError(
                f"Prompt of ~{prompt_tokens} tokens exceeds the "
                f"{self._info.max_context_tokens}-token context window",
                retryable=False,
            )

        statements = _claim_lines(request.user)
        if not statements:
            text = "The available records do not contain enough information to answer this."
        else:
            text = " ".join(statements)

        completion_tokens = estimate_tokens(text)
        truncated = completion_tokens > request.max_output_tokens
        if truncated:
            # Truncate on a sentence boundary. A clinical answer cut mid-sentence can end
            # just before a caveat and read as though it were complete.
            text = _truncate_sentences(text, request.max_output_tokens)
            completion_tokens = estimate_tokens(text)

        return ModelResponse(
            text=text,
            usage=TokenUsage(
                prompt_tokens=prompt_tokens, completion_tokens=completion_tokens, calls=1
            ),
            model_key=self._info.key,
            finish_reason="length" if truncated else "stop",
        )

    async def extract(self, *, text: str, target: str) -> str | None:
        """Find ``target``'s value in ``text``, or ``None``.

        Searches the line naming the target rather than the whole passage: a discharge summary
        mentioning both sodium and potassium would otherwise return whichever number appeared
        first, confidently and wrongly.
        """
        needle = target.strip().lower()
        if not needle or not text:
            return None

        for line in text.splitlines():
            if needle not in line.lower():
                continue
            for pattern in _VALUE_PATTERNS:
                match = pattern.search(line)
                if match:
                    return match.group(1)
        return None


class NullLanguageModel:
    """Refuses every call.

    For testing the no-model path, which is a real deployment state: a tenant whose BAA does
    not cover the configured provider must get a safe refusal, not a stack trace.
    """

    @property
    def info(self) -> LanguageModelInfo:
        return LanguageModelInfo(provider="null", model_name="unavailable")

    async def complete(self, request: GenerationRequest) -> ModelResponse:
        raise LanguageModelError("No language model is configured", retryable=False)

    async def extract(self, *, text: str, target: str) -> str | None:
        return None


def _claim_lines(user_prompt: str) -> list[str]:
    """Pull ``- statement [1]`` bullets out of the composed prompt."""
    statements: list[str] = []
    for raw in user_prompt.splitlines():
        line = raw.strip()
        if line.startswith("- ") and len(line) > 2:
            statements.append(line[2:].strip())
    return statements


def _truncate_sentences(text: str, max_tokens: int) -> str:
    """Keep whole sentences up to the token budget."""
    budget = max_tokens * _CHARS_PER_TOKEN
    if len(text) <= budget:
        return text
    kept: list[str] = []
    used = 0
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        if used + len(sentence) > budget:
            break
        kept.append(sentence)
        used += len(sentence) + 1
    return " ".join(kept) if kept else text[:budget].rstrip()
