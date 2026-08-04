"""Local, dependency-free embedding provider.

**What this is.** A hashed lexical embedder: text is tokenised, expanded with character
n-grams, hashed into a fixed-width vector with sublinear term weighting, and L2-normalised.
It produces genuine similarity structure — documents sharing clinical vocabulary land near
each other — so the entire retrieval pipeline can be exercised, benchmarked, and *evaluated*
without a model download, an API key, or network access.

**What this is not.** It is not a clinical embedding model. It has no notion that
"myocardial infarction" and "heart attack" are the same thing, because it has no semantics
beyond surface form. Anywhere real semantic retrieval matters, this is a baseline to beat,
not a model to ship. ``Settings`` refuses it in deployed environments for exactly that
reason.

Its value is that it is a *fair* baseline. The evaluation framework scores it like any other
provider, so when a real model is selected the comparison is quantitative rather than
assumed — which is the point of the bake-off in
docs/architecture/02-rag-hybrid-retrieval.md §1.3.

Character n-grams matter more here than they would in general text: clinical writing is full
of morphological variation ("cardiac"/"cardiomyopathy") and abbreviations, and subword
overlap recovers some of the matching that whole-token hashing alone would miss.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter

from cip_retrieval.embeddings.base import EmbeddingModelInfo, InputKind

__all__ = ["HashingEmbeddingProvider"]

_TOKEN_PATTERN = re.compile(r"[a-z0-9]+(?:[./-][a-z0-9]+)*")
"""Keeps clinical tokens intact: '0.04', 'mg/dl', 'covid-19' are one token each, not three."""

_DEFAULT_DIMENSIONS = 384


class HashingEmbeddingProvider:
    """Deterministic hashed-lexical embeddings.

    Deterministic across processes and runs: the hash is SHA-1 of the token, not Python's
    randomised ``hash()``. Without that, vectors written by one worker would be
    incomparable with vectors written by another — an index-corrupting bug that only
    appears in multi-process deployments.
    """

    def __init__(
        self,
        *,
        dimensions: int = _DEFAULT_DIMENSIONS,
        char_ngram_size: int = 4,
        use_char_ngrams: bool = True,
    ) -> None:
        if dimensions < 16:
            raise ValueError("dimensions must be >= 16 for usable separation")
        if char_ngram_size < 2:
            raise ValueError("char_ngram_size must be >= 2")
        self._dimensions = dimensions
        self._char_ngram_size = char_ngram_size
        self._use_char_ngrams = use_char_ngrams

    @property
    def info(self) -> EmbeddingModelInfo:
        return EmbeddingModelInfo(
            provider="local",
            model_name=f"hashing-lexical-n{self._char_ngram_size}",
            dimensions=self._dimensions,
            max_input_tokens=1_000_000,
            supports_input_kind=False,
        )

    async def embed(
        self, texts: list[str], *, kind: InputKind = InputKind.PASSAGE
    ) -> list[tuple[float, ...]]:
        """Embed texts. Pure CPU and fast enough to run inline without a thread pool."""
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> tuple[float, ...]:
        counts = self._feature_counts(text)
        if not counts:
            # An all-stopword or empty input still needs a valid vector. A zero vector has
            # undefined cosine similarity, so emit a fixed unit vector instead: it is
            # equidistant from everything rather than crashing the similarity computation.
            return tuple([1.0 / math.sqrt(self._dimensions)] * self._dimensions)

        vector = [0.0] * self._dimensions
        for feature, count in counts.items():
            index, sign = self._bucket(feature)
            # Sublinear term frequency: a term appearing 50 times is more relevant than one
            # appearing twice, but not 25x more. Raw counts let a repeated boilerplate term
            # dominate a chunk's entire vector.
            vector[index] += sign * (1.0 + math.log(count))

        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0.0:
            # Signed buckets can cancel exactly. Rare, but a zero vector downstream is a
            # divide-by-zero in cosine similarity, so fall back to the uniform vector.
            return tuple([1.0 / math.sqrt(self._dimensions)] * self._dimensions)
        return tuple(value / norm for value in vector)

    def _feature_counts(self, text: str) -> Counter[str]:
        """Tokens plus character n-grams, counted."""
        tokens = _TOKEN_PATTERN.findall(text.lower())
        features: Counter[str] = Counter(tokens)

        if self._use_char_ngrams:
            size = self._char_ngram_size
            for token in tokens:
                if len(token) <= size:
                    continue
                padded = f"^{token}$"
                for start in range(len(padded) - size + 1):
                    features[f"#{padded[start : start + size]}"] += 1
        return features

    def _bucket(self, feature: str) -> tuple[int, float]:
        """Map a feature to a bucket and a sign.

        The signed-hashing trick (one hash bit chooses +1/-1) makes collisions cancel on
        average instead of always adding, which measurably reduces the similarity inflation
        that unsigned hashing produces between unrelated texts.
        """
        digest = hashlib.sha1(feature.encode("utf-8"), usedforsecurity=False).digest()
        index = int.from_bytes(digest[:4], "big") % self._dimensions
        sign = 1.0 if digest[4] & 1 else -1.0
        return index, sign
