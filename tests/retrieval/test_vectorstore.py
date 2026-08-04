"""Vector store tests.

Two things carry the weight here: tenant isolation (a leak is a PHI incident) and the
guarantee that filters are applied *before* similarity ranking rather than after. The Atlas
pipeline is asserted structurally, because a misplaced ``tenant_id`` filter is invisible in
behavioural tests against a stub but is a live tenant leak in production.
"""

from __future__ import annotations

import math
import uuid

import pytest

from cip_retrieval.vectorstore import (
    InMemoryVectorStore,
    MongoAtlasVectorStore,
    VectorQuery,
    VectorRecord,
    cosine_similarity,
    to_unit_interval,
)

TENANT_A = uuid.UUID("11111111-1111-4111-8111-111111111111")
TENANT_B = uuid.UUID("22222222-2222-4222-8222-222222222222")
MODEL = "local/hashing-lexical-n4/8"


def _record(
    record_id: str,
    values: tuple[float, ...],
    *,
    tenant_id: uuid.UUID = TENANT_A,
    model_key: str = MODEL,
    **overrides: object,
) -> VectorRecord:
    fields: dict[str, object] = {
        "id": record_id,
        "tenant_id": tenant_id,
        "values": values,
        "model_key": model_key,
        "text": f"text for {record_id}",
    }
    fields.update(overrides)
    return VectorRecord(**fields)  # type: ignore[arg-type]


def _unit(*values: float) -> tuple[float, ...]:
    norm = math.sqrt(sum(v * v for v in values))
    return tuple(v / norm for v in values)


class TestCosineSimilarity:
    def test_identical_vectors_score_one(self) -> None:
        assert math.isclose(cosine_similarity((1.0, 0.0), (1.0, 0.0)), 1.0)

    def test_orthogonal_vectors_score_zero(self) -> None:
        assert math.isclose(cosine_similarity((1.0, 0.0), (0.0, 1.0)), 0.0, abs_tol=1e-12)

    def test_opposite_vectors_score_minus_one(self) -> None:
        assert math.isclose(cosine_similarity((1.0, 0.0), (-1.0, 0.0)), -1.0)

    def test_zero_vector_scores_zero_rather_than_dividing_by_zero(self) -> None:
        assert cosine_similarity((0.0, 0.0), (1.0, 0.0)) == 0.0

    def test_dimension_mismatch_raises(self) -> None:
        """Comparing vectors from different models must fail, not return a plausible score."""
        with pytest.raises(ValueError, match="Dimension mismatch"):
            cosine_similarity((1.0, 0.0), (1.0, 0.0, 0.0))

    @pytest.mark.parametrize(("similarity", "expected"), [(-1.0, 0.0), (0.0, 0.5), (1.0, 1.0)])
    def test_unit_interval_mapping(self, similarity: float, expected: float) -> None:
        assert math.isclose(to_unit_interval(similarity), expected)


class TestVectorRecordValidation:
    def test_rejects_an_empty_vector(self) -> None:
        with pytest.raises(ValueError, match="must not be empty"):
            _record("a", ())

    def test_requires_a_model_key(self) -> None:
        """A vector without model identity cannot be safely compared to anything."""
        with pytest.raises(ValueError, match="model_key"):
            _record("a", (1.0,), model_key="")


class TestVectorQueryValidation:
    def test_rejects_an_empty_query_vector(self) -> None:
        with pytest.raises(ValueError, match="must not be empty"):
            VectorQuery(values=(), tenant_id=TENANT_A, model_key=MODEL)

    def test_rejects_non_positive_top_k(self) -> None:
        with pytest.raises(ValueError, match="top_k"):
            VectorQuery(values=(1.0,), tenant_id=TENANT_A, model_key=MODEL, top_k=0)

    @pytest.mark.parametrize("min_score", [-0.1, 1.1])
    def test_rejects_out_of_range_threshold(self, min_score: float) -> None:
        with pytest.raises(ValueError, match="min_score"):
            VectorQuery(values=(1.0,), tenant_id=TENANT_A, model_key=MODEL, min_score=min_score)


class TestInMemoryVectorStore:
    @pytest.fixture
    async def store(self) -> InMemoryVectorStore:
        store = InMemoryVectorStore()
        await store.upsert(
            [
                _record("a", _unit(1.0, 0.0, 0.0)),
                _record("b", _unit(0.9, 0.1, 0.0)),
                _record("c", _unit(0.0, 1.0, 0.0)),
                _record("d", _unit(0.0, 0.0, 1.0)),
            ]
        )
        return store

    async def test_returns_nearest_first(self, store: InMemoryVectorStore) -> None:
        matches = await store.search(
            VectorQuery(values=_unit(1.0, 0.0, 0.0), tenant_id=TENANT_A, model_key=MODEL, top_k=2)
        )
        assert [m.record.id for m in matches] == ["a", "b"]
        assert matches[0].score >= matches[1].score

    async def test_respects_top_k(self, store: InMemoryVectorStore) -> None:
        matches = await store.search(
            VectorQuery(values=_unit(1.0, 0.0, 0.0), tenant_id=TENANT_A, model_key=MODEL, top_k=1)
        )
        assert len(matches) == 1

    async def test_scores_are_in_the_unit_interval(self, store: InMemoryVectorStore) -> None:
        matches = await store.search(
            VectorQuery(values=_unit(1.0, 1.0, 1.0), tenant_id=TENANT_A, model_key=MODEL, top_k=4)
        )
        assert all(0.0 <= m.score <= 1.0 for m in matches)

    async def test_upsert_replaces_rather_than_duplicates(self, store: InMemoryVectorStore) -> None:
        """Re-indexing must replace a chunk's vector, not add a competing copy."""
        await store.upsert([_record("a", _unit(0.0, 0.0, 1.0))])
        assert await store.count(tenant_id=TENANT_A) == 4

        matches = await store.search(
            VectorQuery(values=_unit(0.0, 0.0, 1.0), tenant_id=TENANT_A, model_key=MODEL, top_k=4)
        )
        assert matches[0].record.id in {"a", "d"}

    async def test_threshold_excludes_weak_matches(self, store: InMemoryVectorStore) -> None:
        """'c' and 'd' are orthogonal to the query and score 0.5 after mapping."""
        matches = await store.search(
            VectorQuery(
                values=_unit(1.0, 0.0, 0.0),
                tenant_id=TENANT_A,
                model_key=MODEL,
                top_k=10,
                min_score=0.9,
            )
        )
        assert [m.record.id for m in matches] == ["a", "b"]

    async def test_threshold_of_one_admits_only_an_exact_match(
        self, store: InMemoryVectorStore
    ) -> None:
        matches = await store.search(
            VectorQuery(
                values=_unit(1.0, 0.0, 0.0),
                tenant_id=TENANT_A,
                model_key=MODEL,
                top_k=10,
                min_score=1.0,
            )
        )
        assert [m.record.id for m in matches] == ["a"]

    async def test_orthogonal_matches_score_half_not_zero(self, store: InMemoryVectorStore) -> None:
        """Documents the threshold scale, which is easy to misread.

        Scores map cosine [-1, 1] onto [0, 1], matching how Atlas reports cosine
        similarity, so a threshold means the same thing against either backend. The
        consequence is that 0.5 means "unrelated", not "moderately related" — a
        ``min_score`` of 0.5 filters almost nothing.
        """
        matches = await store.search(
            VectorQuery(values=_unit(1.0, 0.0, 0.0), tenant_id=TENANT_A, model_key=MODEL, top_k=10)
        )
        orthogonal = next(m for m in matches if m.record.id == "c")
        assert math.isclose(orthogonal.score, 0.5, abs_tol=1e-9)

    async def test_delete_document_removes_only_its_vectors(self) -> None:
        store = InMemoryVectorStore()
        doc_one, doc_two = uuid.uuid4(), uuid.uuid4()
        await store.upsert(
            [
                _record("a", _unit(1.0, 0.0), document_id=doc_one),
                _record("b", _unit(0.0, 1.0), document_id=doc_one),
                _record("c", _unit(1.0, 1.0), document_id=doc_two),
            ]
        )
        assert await store.delete_document(doc_one, tenant_id=TENANT_A) == 2
        assert await store.count(tenant_id=TENANT_A) == 1

    async def test_health_check_reports_backend_and_size(self, store: InMemoryVectorStore) -> None:
        health = await store.health_check()
        assert health["status"] == "ok"
        assert health["vectors"] == 4


class TestTenantIsolation:
    """A cross-tenant match is a PHI incident, not a relevance bug."""

    @pytest.fixture
    async def store(self) -> InMemoryVectorStore:
        store = InMemoryVectorStore()
        # Tenant B's vector is an exact match for the query; tenant A's is a poor one.
        # A leak would therefore rank first, making this test fail loudly.
        await store.upsert(
            [
                _record("a-far", _unit(0.0, 1.0), tenant_id=TENANT_A),
                _record("b-exact", _unit(1.0, 0.0), tenant_id=TENANT_B),
            ]
        )
        return store

    async def test_search_never_crosses_tenants(self, store: InMemoryVectorStore) -> None:
        matches = await store.search(
            VectorQuery(values=_unit(1.0, 0.0), tenant_id=TENANT_A, model_key=MODEL, top_k=10)
        )
        assert [m.record.id for m in matches] == ["a-far"]

    async def test_count_is_per_tenant(self, store: InMemoryVectorStore) -> None:
        assert await store.count(tenant_id=TENANT_A) == 1
        assert await store.count(tenant_id=TENANT_B) == 1

    async def test_delete_does_not_reach_another_tenant(self) -> None:
        store = InMemoryVectorStore()
        shared_doc = uuid.uuid4()
        await store.upsert(
            [
                _record("a", _unit(1.0, 0.0), tenant_id=TENANT_A, document_id=shared_doc),
                _record("b", _unit(1.0, 0.0), tenant_id=TENANT_B, document_id=shared_doc),
            ]
        )
        assert await store.delete_document(shared_doc, tenant_id=TENANT_A) == 1
        assert await store.count(tenant_id=TENANT_B) == 1


class TestModelIsolation:
    async def test_vectors_from_another_model_are_excluded(self) -> None:
        """Mixing models silently returns nonsense rankings rather than failing."""
        store = InMemoryVectorStore()
        await store.upsert(
            [
                _record("current", _unit(0.0, 1.0), model_key="model-v2/384"),
                _record("stale", _unit(1.0, 0.0), model_key="model-v1/384"),
            ]
        )
        matches = await store.search(
            VectorQuery(
                values=_unit(1.0, 0.0), tenant_id=TENANT_A, model_key="model-v2/384", top_k=10
            )
        )
        assert [m.record.id for m in matches] == ["current"]


class TestMetadataFilters:
    @pytest.fixture
    async def store(self) -> InMemoryVectorStore:
        store = InMemoryVectorStore()
        patient = uuid.UUID("33333333-3333-4333-8333-333333333333")
        await store.upsert(
            [
                _record(
                    "lab",
                    _unit(1.0, 0.0),
                    document_type="lab_report",
                    section_name="laboratory_results",
                    patient_id=patient,
                ),
                _record(
                    "discharge",
                    _unit(1.0, 0.0),
                    document_type="discharge_summary",
                    section_name="hospital_course",
                ),
            ]
        )
        return store

    async def test_filters_by_document_type(self, store: InMemoryVectorStore) -> None:
        matches = await store.search(
            VectorQuery(
                values=_unit(1.0, 0.0),
                tenant_id=TENANT_A,
                model_key=MODEL,
                document_types=("lab_report",),
            )
        )
        assert [m.record.id for m in matches] == ["lab"]

    async def test_filters_by_section(self, store: InMemoryVectorStore) -> None:
        matches = await store.search(
            VectorQuery(
                values=_unit(1.0, 0.0),
                tenant_id=TENANT_A,
                model_key=MODEL,
                section_names=("hospital_course",),
            )
        )
        assert [m.record.id for m in matches] == ["discharge"]

    async def test_filters_by_patient(self, store: InMemoryVectorStore) -> None:
        """Patient scoping is a filter, not a hint — the wrong patient is a PHI incident."""
        patient = uuid.UUID("33333333-3333-4333-8333-333333333333")
        matches = await store.search(
            VectorQuery(
                values=_unit(1.0, 0.0),
                tenant_id=TENANT_A,
                model_key=MODEL,
                patient_id=patient,
            )
        )
        assert [m.record.id for m in matches] == ["lab"]


class TestAtlasPipeline:
    """Structural assertions on the aggregation Atlas will run.

    A misplaced filter cannot be caught behaviourally without a real cluster, but it is
    exactly the mistake that leaks tenants — so the pipeline shape is asserted directly.
    """

    @pytest.fixture
    def store(self) -> MongoAtlasVectorStore:
        return MongoAtlasVectorStore(collection=object())

    def _stage(self, store: MongoAtlasVectorStore, query: VectorQuery) -> dict:
        return store._build_pipeline(query)[0]["$vectorSearch"]

    def test_tenant_filter_is_inside_vector_search_not_a_later_match(
        self, store: MongoAtlasVectorStore
    ) -> None:
        """Post-filtering an ANN top-K can return nothing for a valid query (finding D5)."""
        pipeline = store._build_pipeline(
            VectorQuery(values=(1.0, 0.0), tenant_id=TENANT_A, model_key=MODEL)
        )
        conditions = pipeline[0]["$vectorSearch"]["filter"]["$and"]
        assert {"tenant_id": {"$eq": str(TENANT_A)}} in conditions
        assert not any("$match" in stage for stage in pipeline)

    def test_model_key_is_always_filtered(self, store: MongoAtlasVectorStore) -> None:
        conditions = self._stage(
            store, VectorQuery(values=(1.0,), tenant_id=TENANT_A, model_key=MODEL)
        )["filter"]["$and"]
        assert {"model_key": {"$eq": MODEL}} in conditions

    def test_optional_filters_are_only_added_when_requested(
        self, store: MongoAtlasVectorStore
    ) -> None:
        conditions = self._stage(
            store, VectorQuery(values=(1.0,), tenant_id=TENANT_A, model_key=MODEL)
        )["filter"]["$and"]
        assert len(conditions) == 2

    def test_all_metadata_filters_are_pushed_down(self, store: MongoAtlasVectorStore) -> None:
        patient, document = uuid.uuid4(), uuid.uuid4()
        conditions = self._stage(
            store,
            VectorQuery(
                values=(1.0,),
                tenant_id=TENANT_A,
                model_key=MODEL,
                patient_id=patient,
                document_types=("lab_report",),
                section_names=("laboratory_results",),
                document_ids=(document,),
            ),
        )["filter"]["$and"]
        assert {"patient_id": {"$eq": str(patient)}} in conditions
        assert {"document_type": {"$in": ["lab_report"]}} in conditions
        assert {"section_name": {"$in": ["laboratory_results"]}} in conditions
        assert {"document_id": {"$in": [str(document)]}} in conditions

    def test_num_candidates_defaults_above_limit(self, store: MongoAtlasVectorStore) -> None:
        """Atlas requires numCandidates > limit; too few collapses recall."""
        stage = self._stage(
            store, VectorQuery(values=(1.0,), tenant_id=TENANT_A, model_key=MODEL, top_k=10)
        )
        assert stage["numCandidates"] > stage["limit"]

    def test_num_candidates_is_capped(self, store: MongoAtlasVectorStore) -> None:
        stage = self._stage(
            store,
            VectorQuery(
                values=(1.0,),
                tenant_id=TENANT_A,
                model_key=MODEL,
                top_k=10,
                num_candidates=999_999,
            ),
        )
        assert stage["numCandidates"] <= 10_000

    def test_index_definition_declares_every_filter_field(self) -> None:
        """Atlas silently ignores filters on undeclared fields — an omission is a leak."""
        definition = MongoAtlasVectorStore.index_definition(dimensions=384)
        declared = {
            field["path"]
            for field in definition["definition"]["fields"]
            if field["type"] == "filter"
        }
        assert {
            "tenant_id",
            "model_key",
            "patient_id",
            "document_id",
            "document_type",
            "section_name",
        } <= declared

    def test_index_definition_uses_cosine_at_the_declared_dimensionality(self) -> None:
        definition = MongoAtlasVectorStore.index_definition(dimensions=384)
        vector_field = next(
            field for field in definition["definition"]["fields"] if field["type"] == "vector"
        )
        assert vector_field["numDimensions"] == 384
        assert vector_field["similarity"] == "cosine"

    def test_uuid_fields_serialise_as_strings(self) -> None:
        """Atlas filters compare scalars; a UUID object would never match."""
        document = MongoAtlasVectorStore._to_document(
            _record("a", (1.0,), document_id=uuid.uuid4())
        )
        assert isinstance(document["tenant_id"], str)
        assert isinstance(document["document_id"], str)

    def test_document_round_trip_preserves_fields(self) -> None:
        original = _record(
            "a",
            (1.0, 2.0),
            document_id=uuid.uuid4(),
            section_name="assessment",
            document_type="discharge_summary",
            page_start=3,
        )
        restored = MongoAtlasVectorStore._from_document(
            MongoAtlasVectorStore._to_document(original)
        )
        assert restored.id == original.id
        assert restored.tenant_id == original.tenant_id
        assert restored.document_id == original.document_id
        assert restored.section_name == original.section_name
        assert restored.page_start == original.page_start
        assert restored.values == original.values
