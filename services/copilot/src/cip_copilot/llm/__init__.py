"""Language-model seam and its deterministic implementation."""

from cip_copilot.llm.base import (
    GenerationRequest,
    LanguageModel,
    LanguageModelError,
    LanguageModelInfo,
    ModelResponse,
)
from cip_copilot.llm.extractive import ExtractiveLanguageModel, NullLanguageModel

__all__ = [
    "ExtractiveLanguageModel",
    "GenerationRequest",
    "LanguageModel",
    "LanguageModelError",
    "LanguageModelInfo",
    "ModelResponse",
    "NullLanguageModel",
]
