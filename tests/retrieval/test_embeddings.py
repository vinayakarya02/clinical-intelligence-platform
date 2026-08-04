"""Embedding pipeline tests.

The properties that matter operationally are determinism (vectors written by different
workers must be comparable), order preservation (a reordering bug silently attaches every
vector to the wrong chunk), and correct retry classification.
"""

from __future__ import annotations

import math

import pytest

from cip_retrieval.embeddings import (
    EmbeddingError,
    EmbeddingModelInfo,
    EmbeddingService,
    EmbeddingServiceOptions,
    HashingEmbeddingProvider,
    InMemoryEmbeddingCache,
    InputKind,
    NullEmbeddingCache,
    content_key,
)


def _cosine(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


class _RecordingProvider:
    """Provider that records its calls and can be scripted to fail."""

    def __init__(
        self,
        *,
        dimensions: int = 32,
        failures: int = 0,
        retryable: bool = True,
        return_wrong_count: bool = False,
    ) -> None:
        self._dimensions = dimensions
        self._remaining_failures = failures
        self._retryable = retryable
        self._return_wrong_count = return_wrong_count
        self.calls: list[list[str]] = []
        self.kinds: list[InputKind] = []

    @property
    def info(self) -> EmbeddingModelInfo:
        return EmbeddingModelInfo(
            provider="test", model_name="recorder", dimensions=self._dimensions
        )

    async def embed(
        self, texts: list[str], *, kind: InputKind = InputKind.PASSAGE
    ) -> list[tuple[float, ...]]:
        self.calls.append(list(texts))
        self.kinds.append(kind)
        if self._remaining_failures > 0:
            self._remaining_failures -= 1
            raise EmbeddingError("simulated provider failure", retryable=self._retryable)
        if self._return_wrong_count:
            return [tuple([1.0] * self._dimensions)]
        return [
            tuple([float(len(text) % 7 + i) for i in range(self._dimensions)]) for text in texts
        ]


@pytest.fixture
def fast_options() -> EmbeddingServiceOptions:
    """No backoff delay, so retry tests do not spend real seconds sleeping."""
    return EmbeddingServiceOptions(
        batch_size=4, max_attempts=3, base_backoff_seconds=0.0, jitter=False
    )


class TestHashingProvider:
    async def test_produces_unit_vectors(self) -> None:
        provider = HashingEmbeddingProvider(dimensions=128)
        vectors = await provider.embed(["acute myocardial infarction"])
        assert math.isclose(math.sqrt(sum(v * v for v in vectors[0])), 1.0, rel_tol=1e-9)

    async def test_respects_requested_dimensionality(self) -> None:
        provider = HashingEmbeddingProvider(dimensions=64)
        vectors = await provider.embed(["text"])
        assert len(vectors[0]) == 64
        assert provider.info.dimensions == 64

    async def test_is_deterministic_across_instances(self) -> None:
        """Different workers must produce comparable vectors or the index is corrupt."""
        text = "Troponin I 0.04 ng/mL within reference range"
        first = await HashingEmbeddingProvider(dimensions=128).embed([text])
        second = await HashingEmbeddingProvider(dimensions=128).embed([text])
        assert first[0] == second[0]

    async def test_related_clinical_text_scores_higher_than_unrelated(self) -> None:
        provider = HashingEmbeddingProvider(dimensions=384)
        vectors = await provider.embed(
            [
                "Patient reports substernal chest pain radiating to the left arm.",
                "Chest pain radiating to the arm, concerning for cardiac ischemia.",
                "Discharge medications include lisinopril 10 mg daily.",
            ]
        )
        related = _cosine(vectors[0], vectors[1])
        unrelated = _cosine(vectors[0], vectors[2])
        assert related > unrelated

    async def test_identical_text_is_maximally_similar(self) -> None:
        provider = HashingEmbeddingProvider(dimensions=256)
        vectors = await provider.embed(["sodium 141 mmol/L", "sodium 141 mmol/L"])
        assert math.isclose(_cosine(vectors[0], vectors[1]), 1.0, rel_tol=1e-9)

    async def test_empty_text_yields_a_valid_unit_vector(self) -> None:
        """A zero vector would make cosine similarity undefined downstream."""
        provider = HashingEmbeddingProvider(dimensions=64)
        vectors = await provider.embed(["", "   "])
        for vector in vectors:
            assert math.isclose(math.sqrt(sum(v * v for v in vector)), 1.0, rel_tol=1e-9)

    async def test_clinical_tokens_survive_tokenisation(self) -> None:
        """'mg/dl' and '0.04' must not be shredded into separate tokens."""
        provider = HashingEmbeddingProvider(dimensions=256, use_char_ngrams=False)
        with_unit = (await provider.embed(["creatinine 1.02 mg/dl"]))[0]
        different_unit = (await provider.embed(["creatinine 1.02 mmol/l"]))[0]
        assert with_unit != different_unit

    @pytest.mark.parametrize("dimensions", [8, 4, 0])
    def test_rejects_unusably_small_dimensionality(self, dimensions: int) -> None:
        with pytest.raises(ValueError, match="dimensions"):
            HashingEmbeddingProvider(dimensions=dimensions)


class TestEmbeddingService:
    async def test_preserves_input_order(self, fast_options: EmbeddingServiceOptions) -> None:
        """A reordering bug attaches every vector to the wrong chunk, silently."""
        provider = _RecordingProvider()
        service = EmbeddingService(provider, options=fast_options)
        texts = [f"text of length {'x' * i}" for i in range(10)]

        batch = await service.embed_texts(texts)

        expected = await _RecordingProvider().embed(texts)
        assert [v.values for v in batch.vectors] == expected

    async def test_batches_according_to_configuration(
        self, fast_options: EmbeddingServiceOptions
    ) -> None:
        provider = _RecordingProvider()
        service = EmbeddingService(provider, options=fast_options)
        await service.embed_texts([f"unique text {i}" for i in range(10)])
        assert len(provider.calls) == 3  # 4 + 4 + 2
        assert all(len(call) <= 4 for call in provider.calls)

    async def test_deduplicates_repeated_text_within_a_request(
        self, fast_options: EmbeddingServiceOptions
    ) -> None:
        """Boilerplate repeated across chunks must not be paid for repeatedly."""
        provider = _RecordingProvider()
        service = EmbeddingService(provider, options=fast_options)

        batch = await service.embed_texts(["same"] * 8)

        embedded = [text for call in provider.calls for text in call]
        assert embedded == ["same"]
        assert len(batch.vectors) == 8
        assert len({v.values for v in batch.vectors}) == 1

    async def test_vectors_carry_the_model_key(self, fast_options: EmbeddingServiceOptions) -> None:
        service = EmbeddingService(_RecordingProvider(), options=fast_options)
        batch = await service.embed_texts(["a"])
        assert batch.vectors[0].model_key == "test/recorder/32"

    async def test_empty_input_makes_no_provider_call(
        self, fast_options: EmbeddingServiceOptions
    ) -> None:
        provider = _RecordingProvider()
        batch = await EmbeddingService(provider, options=fast_options).embed_texts([])
        assert len(batch) == 0
        assert provider.calls == []

    async def test_query_embedding_uses_the_query_input_kind(
        self, fast_options: EmbeddingServiceOptions
    ) -> None:
        """Asymmetric models encode queries differently; passing the wrong kind hurts recall."""
        provider = _RecordingProvider()
        service = EmbeddingService(provider, options=fast_options)
        await service.embed_query("what was the troponin")
        assert provider.kinds == [InputKind.QUERY]

    async def test_provider_returning_wrong_count_raises(
        self, fast_options: EmbeddingServiceOptions
    ) -> None:
        """Silently accepting a short response corrupts every subsequent alignment."""
        service = EmbeddingService(
            _RecordingProvider(return_wrong_count=True), options=fast_options
        )
        with pytest.raises(EmbeddingError, match="vectors for"):
            await service.embed_texts(["a", "b", "c"])


class TestRetry:
    async def test_retries_a_retryable_failure_and_succeeds(
        self, fast_options: EmbeddingServiceOptions
    ) -> None:
        provider = _RecordingProvider(failures=2, retryable=True)
        service = EmbeddingService(provider, options=fast_options)
        batch = await service.embed_texts(["text"])
        assert len(batch) == 1
        assert len(provider.calls) == 3

    async def test_does_not_retry_a_permanent_failure(
        self, fast_options: EmbeddingServiceOptions
    ) -> None:
        """Retrying a permanent error burns the budget and delays the real diagnosis."""
        provider = _RecordingProvider(failures=5, retryable=False)
        service = EmbeddingService(provider, options=fast_options)
        with pytest.raises(EmbeddingError):
            await service.embed_texts(["text"])
        assert len(provider.calls) == 1

    async def test_gives_up_after_max_attempts(self, fast_options: EmbeddingServiceOptions) -> None:
        provider = _RecordingProvider(failures=99, retryable=True)
        service = EmbeddingService(provider, options=fast_options)
        with pytest.raises(EmbeddingError):
            await service.embed_texts(["text"])
        assert len(provider.calls) == fast_options.max_attempts


class TestCaching:
    async def test_second_call_is_served_entirely_from_cache(
        self, fast_options: EmbeddingServiceOptions
    ) -> None:
        provider = _RecordingProvider()
        service = EmbeddingService(provider, cache=InMemoryEmbeddingCache(), options=fast_options)
        texts = ["alpha", "beta", "gamma"]

        first = await service.embed_texts(texts)
        second = await service.embed_texts(texts)

        assert first.cache_hits == 0
        assert second.cache_hits == len(texts)
        assert second.provider_calls == 0
        assert [v.values for v in second.vectors] == [v.values for v in first.vectors]

    async def test_partial_cache_hits_keep_order(
        self, fast_options: EmbeddingServiceOptions
    ) -> None:
        """The riskiest cache path: interleaved hits and misses must not reorder results."""
        service = EmbeddingService(
            _RecordingProvider(), cache=InMemoryEmbeddingCache(), options=fast_options
        )
        await service.embed_texts(["b", "d"])

        batch = await service.embed_texts(["a", "b", "c", "d", "e"])
        reference = await EmbeddingService(_RecordingProvider(), options=fast_options).embed_texts(
            ["a", "b", "c", "d", "e"]
        )

        assert [v.values for v in batch.vectors] == [v.values for v in reference.vectors]
        assert batch.cache_hits == 2

    async def test_cache_key_is_scoped_to_the_model(self) -> None:
        """A model upgrade must miss the cache, not serve vectors from the old model."""
        assert content_key("text", "provider/model-a/384") != content_key(
            "text", "provider/model-b/384"
        )

    async def test_cache_key_is_content_addressed(self) -> None:
        assert content_key("same", "m/1/8") == content_key("same", "m/1/8")
        assert content_key("a", "m/1/8") != content_key("b", "m/1/8")

    async def test_null_cache_never_serves_a_hit(
        self, fast_options: EmbeddingServiceOptions
    ) -> None:
        service = EmbeddingService(
            _RecordingProvider(), cache=NullEmbeddingCache(), options=fast_options
        )
        await service.embed_texts(["x"])
        batch = await service.embed_texts(["x"])
        assert batch.cache_hits == 0

    async def test_lru_evicts_the_oldest_entry(self) -> None:
        cache = InMemoryEmbeddingCache(max_entries=2)
        await cache.set("a", (1.0,))
        await cache.set("b", (2.0,))
        await cache.get("a")  # refresh 'a' so 'b' becomes the eviction target
        await cache.set("c", (3.0,))

        assert await cache.get("a") is not None
        assert await cache.get("b") is None
        assert await cache.get("c") is not None

    async def test_reports_hit_rate(self) -> None:
        cache = InMemoryEmbeddingCache()
        await cache.set("k", (1.0,))
        await cache.get("k")
        await cache.get("missing")
        assert cache.stats["hits"] == 1
        assert cache.stats["misses"] == 1
        assert cache.stats["hit_rate"] == 0.5


class TestInputKindCacheIsolation:
    """Regression: the cache key ignored InputKind."""

    async def test_a_query_does_not_reuse_a_passage_vector(self) -> None:
        """Asymmetric models encode a query and a passage differently.

        Keying the cache on content alone served the passage vector for an identical query
        string, silently substituting the wrong encoding — the exact failure `InputKind`
        exists to prevent, and one that surfaces only as degraded recall.
        """
        provider = _KindSensitiveProvider()
        service = EmbeddingService(provider, cache=InMemoryEmbeddingCache())

        passage = await service.embed_texts(["potassium 5.4"])
        query = await service.embed_query("potassium 5.4")

        assert passage.vectors[0].values != query.values
        assert provider.calls == [InputKind.PASSAGE, InputKind.QUERY]

    async def test_the_same_kind_still_hits_the_cache(self) -> None:
        provider = _KindSensitiveProvider()
        service = EmbeddingService(provider, cache=InMemoryEmbeddingCache())

        await service.embed_query("potassium 5.4")
        second = await service.embed_texts(["potassium 5.4"], kind=InputKind.QUERY)

        assert second.cache_hits == 1
        assert provider.calls == [InputKind.QUERY]


class _KindSensitiveProvider:
    """Encodes queries and passages differently, as an asymmetric model does."""

    def __init__(self) -> None:
        self.calls: list[InputKind] = []

    @property
    def info(self) -> EmbeddingModelInfo:
        return EmbeddingModelInfo(provider="test", model_name="asymmetric", dimensions=2)

    async def embed(
        self, texts: list[str], *, kind: InputKind = InputKind.PASSAGE
    ) -> list[tuple[float, ...]]:
        self.calls.append(kind)
        offset = 0.0 if kind is InputKind.PASSAGE else 1.0
        return [(offset, float(len(text))) for text in texts]
