"""Knowledge graph tests.

The invariants under test are the ones that make graph evidence usable in a clinical
setting: tenant scoping, mandatory provenance on actionable edges, idempotent re-ingestion,
and bounded traversal that decays confidence with distance.
"""

from __future__ import annotations

import uuid

import pytest

from cip_retrieval.graph import (
    ACTIONABLE_RELATIONSHIPS,
    GraphNode,
    GraphRelationship,
    InMemoryGraphStore,
    NodeLabel,
    Provenance,
    RelationshipType,
    TraversalOptions,
    is_ontology_label,
    is_patient_scoped,
    traverse,
)

TENANT_A = uuid.UUID("11111111-1111-4111-8111-111111111111")
TENANT_B = uuid.UUID("22222222-2222-4222-8222-222222222222")
DOCUMENT = uuid.UUID("44444444-4444-4444-8444-444444444444")


def _sourced() -> Provenance:
    return Provenance(source_document_id=DOCUMENT, evidence_level="label")


class TestSchemaClassification:
    @pytest.mark.parametrize(
        "label",
        [NodeLabel.PATIENT, NodeLabel.CONDITION, NodeLabel.MEDICATION, NodeLabel.OBSERVATION],
    )
    def test_clinical_instances_are_tenant_scoped(self, label: NodeLabel) -> None:
        assert is_patient_scoped(label)
        assert not is_ontology_label(label)

    @pytest.mark.parametrize(
        "label",
        [NodeLabel.SNOMED_CONCEPT, NodeLabel.RXNORM_CONCEPT, NodeLabel.GUIDELINE],
    )
    def test_reference_data_is_shared(self, label: NodeLabel) -> None:
        """Shared concepts must not be duplicated per tenant."""
        assert is_ontology_label(label)
        assert not is_patient_scoped(label)

    def test_actionable_relationships_are_the_clinically_decisive_ones(self) -> None:
        assert RelationshipType.CONTRAINDICATED_WITH in ACTIONABLE_RELATIONSHIPS
        assert RelationshipType.CAUSES in ACTIONABLE_RELATIONSHIPS
        assert RelationshipType.INTERACTS_WITH in ACTIONABLE_RELATIONSHIPS
        # Structural edges carry no clinical claim and need no provenance.
        assert RelationshipType.HAS_ENCOUNTER not in ACTIONABLE_RELATIONSHIPS


class TestNodeValidation:
    def test_patient_scoped_node_requires_a_tenant(self) -> None:
        with pytest.raises(ValueError, match="requires a tenant_id"):
            GraphNode(NodeLabel.PATIENT, "pat:1")

    def test_ontology_node_rejects_a_tenant(self) -> None:
        """Tenant-scoping shared concepts would duplicate the ontology per tenant."""
        with pytest.raises(ValueError, match="must not carry a tenant_id"):
            GraphNode(NodeLabel.SNOMED_CONCEPT, "sct:1", tenant_id=TENANT_A)

    def test_empty_key_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="key"):
            GraphNode(NodeLabel.SNOMED_CONCEPT, "   ")

    def test_merge_key_includes_tenant_for_scoped_nodes(self) -> None:
        node = GraphNode(NodeLabel.MEDICATION, "med:1", tenant_id=TENANT_A)
        assert node.merge_key == {"key": "med:1", "tenant_id": str(TENANT_A)}

    def test_merge_key_omits_tenant_for_shared_nodes(self) -> None:
        assert GraphNode(NodeLabel.RXNORM_CONCEPT, "rx:1").merge_key == {"key": "rx:1"}


class TestRelationshipValidation:
    @pytest.mark.parametrize("relationship_type", sorted(ACTIONABLE_RELATIONSHIPS))
    def test_actionable_edges_require_provenance(self, relationship_type: RelationshipType) -> None:
        """An unattributable clinical claim cannot be reviewed or defended."""
        with pytest.raises(ValueError, match="requires provenance"):
            GraphRelationship(
                relationship_type,
                NodeLabel.RXNORM_CONCEPT,
                "rx:1",
                NodeLabel.RXNORM_CONCEPT,
                "rx:2",
            )

    def test_actionable_edges_accept_sourced_provenance(self) -> None:
        relationship = GraphRelationship(
            RelationshipType.CAUSES,
            NodeLabel.RXNORM_CONCEPT,
            "rx:1",
            NodeLabel.ADVERSE_EVENT_DEFINITION,
            "ae:1",
            provenance=_sourced(),
        )
        assert relationship.provenance is not None

    def test_empty_provenance_does_not_satisfy_the_requirement(self) -> None:
        """A Provenance object with no source is not provenance."""
        with pytest.raises(ValueError, match="requires provenance"):
            GraphRelationship(
                RelationshipType.TREATS,
                NodeLabel.RXNORM_CONCEPT,
                "rx:1",
                NodeLabel.SNOMED_CONCEPT,
                "sct:1",
                provenance=Provenance(),
            )

    def test_structural_edges_need_no_provenance(self) -> None:
        relationship = GraphRelationship(
            RelationshipType.HAS_ENCOUNTER,
            NodeLabel.PATIENT,
            "pat:1",
            NodeLabel.ENCOUNTER,
            "enc:1",
            tenant_id=TENANT_A,
        )
        assert relationship.provenance is None

    @pytest.mark.parametrize("confidence", [-0.1, 1.1])
    def test_confidence_must_be_a_probability(self, confidence: float) -> None:
        with pytest.raises(ValueError, match="confidence"):
            GraphRelationship(
                RelationshipType.HAS_ENCOUNTER,
                NodeLabel.PATIENT,
                "pat:1",
                NodeLabel.ENCOUNTER,
                "enc:1",
                tenant_id=TENANT_A,
                confidence=confidence,
            )


class TestGraphStore:
    @pytest.fixture
    async def store(self) -> InMemoryGraphStore:
        store = InMemoryGraphStore()
        await store.upsert_nodes(
            [
                GraphNode(
                    NodeLabel.RXNORM_CONCEPT,
                    "rx:lisinopril",
                    properties={"display_text": "Lisinopril"},
                ),
                GraphNode(
                    NodeLabel.RXNORM_CONCEPT,
                    "rx:spironolactone",
                    properties={"display_text": "Spironolactone"},
                ),
                GraphNode(
                    NodeLabel.SNOMED_CONCEPT,
                    "sct:hyperkalemia",
                    properties={"display_text": "Hyperkalemia"},
                ),
                GraphNode(
                    NodeLabel.MEDICATION,
                    "med:1",
                    tenant_id=TENANT_A,
                    properties={"display_text": "Lisinopril 10 mg daily"},
                ),
            ]
        )
        await store.upsert_relationships(
            [
                GraphRelationship(
                    RelationshipType.HAS_RXNORM_CONCEPT,
                    NodeLabel.MEDICATION,
                    "med:1",
                    NodeLabel.RXNORM_CONCEPT,
                    "rx:lisinopril",
                    tenant_id=TENANT_A,
                ),
                GraphRelationship(
                    RelationshipType.INTERACTS_WITH,
                    NodeLabel.RXNORM_CONCEPT,
                    "rx:lisinopril",
                    NodeLabel.RXNORM_CONCEPT,
                    "rx:spironolactone",
                    confidence=0.9,
                    provenance=_sourced(),
                ),
                GraphRelationship(
                    RelationshipType.CAUSES,
                    NodeLabel.RXNORM_CONCEPT,
                    "rx:spironolactone",
                    NodeLabel.SNOMED_CONCEPT,
                    "sct:hyperkalemia",
                    confidence=0.8,
                    provenance=_sourced(),
                ),
            ]
        )
        return store

    async def test_finds_nodes_by_display_text(self, store: InMemoryGraphStore) -> None:
        found = await store.find_nodes(tenant_id=TENANT_A, text="lisinopril")
        assert {node.key for node in found} >= {"rx:lisinopril"}

    async def test_shared_ontology_is_visible_to_every_tenant(
        self, store: InMemoryGraphStore
    ) -> None:
        """One drug-interaction fact must serve all tenants, not be duplicated per tenant."""
        found = await store.find_nodes(tenant_id=TENANT_B, label=NodeLabel.RXNORM_CONCEPT)
        assert {node.key for node in found} == {"rx:lisinopril", "rx:spironolactone"}

    async def test_patient_data_is_not_visible_to_another_tenant(
        self, store: InMemoryGraphStore
    ) -> None:
        assert await store.find_nodes(tenant_id=TENANT_B, label=NodeLabel.MEDICATION) == []

    async def test_reingestion_merges_rather_than_duplicates(
        self, store: InMemoryGraphStore
    ) -> None:
        """Re-running the pipeline must update nodes, not multiply them."""
        before = (await store.health_check())["nodes"]
        await store.upsert_nodes(
            [GraphNode(NodeLabel.RXNORM_CONCEPT, "rx:lisinopril", properties={"atc": "C09AA03"})]
        )
        after = await store.health_check()
        assert after["nodes"] == before

        found = await store.find_nodes(tenant_id=TENANT_A, text="lisinopril")
        node = next(n for n in found if n.key == "rx:lisinopril")
        assert node.properties["atc"] == "C09AA03"
        assert node.properties["display_text"] == "Lisinopril", "existing properties must survive"

    async def test_repeated_relationship_upsert_does_not_duplicate(
        self, store: InMemoryGraphStore
    ) -> None:
        before = (await store.health_check())["relationships"]
        await store.upsert_relationships(
            [
                GraphRelationship(
                    RelationshipType.INTERACTS_WITH,
                    NodeLabel.RXNORM_CONCEPT,
                    "rx:lisinopril",
                    NodeLabel.RXNORM_CONCEPT,
                    "rx:spironolactone",
                    confidence=0.95,
                    provenance=_sourced(),
                )
            ]
        )
        assert (await store.health_check())["relationships"] == before

    async def test_neighbours_are_returned_with_provenance(self, store: InMemoryGraphStore) -> None:
        edges = await store.neighbours(
            label=NodeLabel.RXNORM_CONCEPT, key="rx:lisinopril", tenant_id=TENANT_A
        )
        interaction = next(
            edge for edge in edges if edge.relationship_type is RelationshipType.INTERACTS_WITH
        )
        assert interaction.source_document_id == DOCUMENT
        assert interaction.confidence == 0.9

    async def test_neighbours_can_be_filtered_by_relationship_type(
        self, store: InMemoryGraphStore
    ) -> None:
        edges = await store.neighbours(
            label=NodeLabel.RXNORM_CONCEPT,
            key="rx:lisinopril",
            tenant_id=TENANT_A,
            relationship_types=(RelationshipType.INTERACTS_WITH,),
        )
        assert {edge.relationship_type for edge in edges} == {RelationshipType.INTERACTS_WITH}


class TestTraversal:
    @pytest.fixture
    async def store(self) -> InMemoryGraphStore:
        store = InMemoryGraphStore()
        await store.upsert_nodes(
            [
                GraphNode(NodeLabel.MEDICATION, "med:1", tenant_id=TENANT_A),
                GraphNode(NodeLabel.RXNORM_CONCEPT, "rx:a"),
                GraphNode(NodeLabel.RXNORM_CONCEPT, "rx:b"),
                GraphNode(NodeLabel.SNOMED_CONCEPT, "sct:c"),
            ]
        )
        await store.upsert_relationships(
            [
                GraphRelationship(
                    RelationshipType.HAS_RXNORM_CONCEPT,
                    NodeLabel.MEDICATION,
                    "med:1",
                    NodeLabel.RXNORM_CONCEPT,
                    "rx:a",
                    tenant_id=TENANT_A,
                ),
                GraphRelationship(
                    RelationshipType.INTERACTS_WITH,
                    NodeLabel.RXNORM_CONCEPT,
                    "rx:a",
                    NodeLabel.RXNORM_CONCEPT,
                    "rx:b",
                    confidence=1.0,
                    provenance=_sourced(),
                ),
                GraphRelationship(
                    RelationshipType.CAUSES,
                    NodeLabel.RXNORM_CONCEPT,
                    "rx:b",
                    NodeLabel.SNOMED_CONCEPT,
                    "sct:c",
                    confidence=1.0,
                    provenance=_sourced(),
                ),
            ]
        )
        return store

    async def test_discovers_multi_hop_clinical_chains(self, store: InMemoryGraphStore) -> None:
        """The reason the graph exists: medication -> interaction -> adverse outcome."""
        paths = await traverse(
            store,
            label=NodeLabel.MEDICATION,
            key="med:1",
            tenant_id=TENANT_A,
            options=TraversalOptions(max_hops=3),
        )
        assert {path.end_key for path in paths} == {"rx:a", "rx:b", "sct:c"}

    async def test_confidence_decays_with_distance(self, store: InMemoryGraphStore) -> None:
        """A chained inference must not outrank the direct evidence it chains."""
        paths = await traverse(
            store,
            label=NodeLabel.MEDICATION,
            key="med:1",
            tenant_id=TENANT_A,
            options=TraversalOptions(max_hops=3),
        )
        by_key = {path.end_key: path for path in paths}
        assert by_key["rx:a"].confidence > by_key["rx:b"].confidence > by_key["sct:c"].confidence

    async def test_hop_limit_is_enforced(self, store: InMemoryGraphStore) -> None:
        paths = await traverse(
            store,
            label=NodeLabel.MEDICATION,
            key="med:1",
            tenant_id=TENANT_A,
            options=TraversalOptions(max_hops=1),
        )
        assert {path.end_key for path in paths} == {"rx:a"}
        assert all(path.hops == 1 for path in paths)

    async def test_results_are_ordered_by_confidence(self, store: InMemoryGraphStore) -> None:
        paths = await traverse(
            store,
            label=NodeLabel.MEDICATION,
            key="med:1",
            tenant_id=TENANT_A,
            options=TraversalOptions(max_hops=3),
        )
        confidences = [path.confidence for path in paths]
        assert confidences == sorted(confidences, reverse=True)

    async def test_confidence_floor_prunes_weak_paths(self, store: InMemoryGraphStore) -> None:
        paths = await traverse(
            store,
            label=NodeLabel.MEDICATION,
            key="med:1",
            tenant_id=TENANT_A,
            options=TraversalOptions(max_hops=3, min_confidence=0.5),
        )
        assert all(path.confidence >= 0.5 for path in paths)
        assert {path.end_key for path in paths} == {"rx:a"}

    async def test_node_budget_bounds_the_traversal(self, store: InMemoryGraphStore) -> None:
        paths = await traverse(
            store,
            label=NodeLabel.MEDICATION,
            key="med:1",
            tenant_id=TENANT_A,
            options=TraversalOptions(max_hops=5, max_total_nodes=2),
        )
        assert len(paths) <= 2

    async def test_a_cycle_does_not_loop_forever(self) -> None:
        """Clinical graphs contain cycles; an unguarded walk would never terminate."""
        store = InMemoryGraphStore()
        await store.upsert_nodes(
            [GraphNode(NodeLabel.RXNORM_CONCEPT, key) for key in ("rx:a", "rx:b")]
        )
        await store.upsert_relationships(
            [
                GraphRelationship(
                    RelationshipType.INTERACTS_WITH,
                    NodeLabel.RXNORM_CONCEPT,
                    "rx:a",
                    NodeLabel.RXNORM_CONCEPT,
                    "rx:b",
                    confidence=1.0,
                    provenance=_sourced(),
                ),
                GraphRelationship(
                    RelationshipType.INTERACTS_WITH,
                    NodeLabel.RXNORM_CONCEPT,
                    "rx:b",
                    NodeLabel.RXNORM_CONCEPT,
                    "rx:a",
                    confidence=1.0,
                    provenance=_sourced(),
                ),
            ]
        )
        paths = await traverse(
            store,
            label=NodeLabel.RXNORM_CONCEPT,
            key="rx:a",
            tenant_id=None,
            options=TraversalOptions(max_hops=10),
        )
        assert {path.end_key for path in paths} == {"rx:b"}

    async def test_paths_render_as_readable_sentences(self, store: InMemoryGraphStore) -> None:
        """Context assembly needs graph evidence as prose, not adjacency tuples."""
        paths = await traverse(
            store,
            label=NodeLabel.MEDICATION,
            key="med:1",
            tenant_id=TENANT_A,
            options=TraversalOptions(max_hops=3),
        )
        deepest = max(paths, key=lambda path: path.hops)
        sentences = deepest.as_sentences()
        assert len(sentences) == deepest.hops
        assert "interacts with" in " ".join(sentences)

    async def test_traversal_does_not_cross_tenants(self) -> None:
        store = InMemoryGraphStore()
        await store.upsert_nodes(
            [
                GraphNode(NodeLabel.MEDICATION, "med:a", tenant_id=TENANT_A),
                GraphNode(NodeLabel.MEDICATION, "med:b", tenant_id=TENANT_B),
            ]
        )
        await store.upsert_relationships(
            [
                GraphRelationship(
                    RelationshipType.HAS_ENCOUNTER,
                    NodeLabel.MEDICATION,
                    "med:a",
                    NodeLabel.MEDICATION,
                    "med:b",
                    tenant_id=TENANT_B,
                )
            ]
        )
        paths = await traverse(store, label=NodeLabel.MEDICATION, key="med:a", tenant_id=TENANT_A)
        assert paths == []

    @pytest.mark.parametrize("max_hops", [0, -1])
    def test_rejects_a_non_positive_hop_limit(self, max_hops: int) -> None:
        with pytest.raises(ValueError, match="max_hops"):
            TraversalOptions(max_hops=max_hops)

    @pytest.mark.parametrize("decay", [0.0, 1.5])
    def test_rejects_an_invalid_decay(self, decay: float) -> None:
        with pytest.raises(ValueError, match="hop_decay"):
            TraversalOptions(hop_decay=decay)


class _RecordingResult:
    """Captures what a Cypher call would return, and records nothing itself."""

    def __init__(self, records: list[dict[str, object]]) -> None:
        self._records = records

    async def single(self) -> dict[str, object] | None:
        return self._records[0] if self._records else None

    def __aiter__(self):  # type: ignore[no-untyped-def]
        async def _generate():  # type: ignore[no-untyped-def]
            for record in self._records:
                yield record

        return _generate()


class _RecordingSession:
    def __init__(self, sink: list[tuple[str, dict[str, object]]]) -> None:
        self._sink = sink

    async def run(self, query: str, **params: object) -> _RecordingResult:
        self._sink.append((query, params))
        # Writes read a single `written` count; reads iterate records. Returning an empty
        # record set for reads keeps the double honest — the assertions are about the query
        # that was generated, not about results it did not produce.
        if "RETURN count(" in query:
            return _RecordingResult([{"written": 0}])
        return _RecordingResult([])


class _RecordingManager:
    """Neo4j manager double that records the Cypher the store generates.

    The Cypher path had no test double at all, which is precisely why two tenant-isolation
    defects survived in it: the in-memory store enforced the rules correctly, so CI was
    green while production would have leaked. Asserting on the generated query is the same
    technique ``TestAtlasPipeline`` uses for ``$vectorSearch``, and it needs no server.
    """

    def __init__(self) -> None:
        self.queries: list[tuple[str, dict[str, object]]] = []

    def write_session(self):  # type: ignore[no-untyped-def]
        return self._session()

    def read_session(self):  # type: ignore[no-untyped-def]
        return self._session()

    def _session(self):  # type: ignore[no-untyped-def]
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _ctx():  # type: ignore[no-untyped-def]
            yield _RecordingSession(self.queries)

        return _ctx()


class TestCypherTenantScoping:
    """Regressions for the two cross-tenant defects found in the Cypher backend."""

    @pytest.fixture
    def manager(self) -> _RecordingManager:
        return _RecordingManager()

    @pytest.fixture
    def store(self, manager: _RecordingManager):  # type: ignore[no-untyped-def]
        from cip_retrieval.graph.store import Neo4jGraphStore

        return Neo4jGraphStore(manager)  # type: ignore[arg-type]

    async def test_patient_scoped_endpoints_are_matched_within_the_tenant(
        self, store, manager: _RecordingManager
    ) -> None:
        """A patient key is unique only within a tenant.

        Matching endpoints on ``key`` alone matched every tenant's node with that key, so
        MERGE attached one tenant's clinical assertion to another tenant's patient.
        """
        await store.upsert_relationships(
            [
                GraphRelationship(
                    type=RelationshipType.DIAGNOSED_WITH,
                    start_label=NodeLabel.PATIENT,
                    start_key="mrn-001",
                    end_label=NodeLabel.CONDITION,
                    end_key="cond-1",
                    tenant_id=TENANT_A,
                    provenance=_sourced(),
                )
            ]
        )
        query, params = manager.queries[0]
        assert "MATCH (a:Patient {key: row.start_key, tenant_id: row.tenant_id})" in query
        assert "MATCH (b:Condition {key: row.end_key, tenant_id: row.tenant_id})" in query
        assert params["rows"][0]["tenant_id"] == str(TENANT_A)  # type: ignore[index]

    async def test_ontology_endpoints_match_on_key_alone(
        self, store, manager: _RecordingManager
    ) -> None:
        """Shared reference data is not tenant-scoped and must not be scoped on write."""
        await store.upsert_relationships(
            [
                GraphRelationship(
                    type=RelationshipType.CONTRAINDICATED_WITH,
                    start_label=NodeLabel.RXNORM_CONCEPT,
                    start_key="rx:a",
                    end_label=NodeLabel.RXNORM_CONCEPT,
                    end_key="rx:b",
                    provenance=_sourced(),
                )
            ]
        )
        query, _ = manager.queries[0]
        assert "MATCH (a:RxNormConcept {key: row.start_key})" in query
        assert "tenant_id: row.tenant_id" not in query.split("MERGE")[0]

    async def test_relationship_tenant_is_persisted(
        self, store, manager: _RecordingManager
    ) -> None:
        """The edge's own tenant was dropped on write, so it could never be filtered on."""
        await store.upsert_relationships(
            [
                GraphRelationship(
                    type=RelationshipType.CONTRAINDICATED_WITH,
                    start_label=NodeLabel.RXNORM_CONCEPT,
                    start_key="rx:a",
                    end_label=NodeLabel.RXNORM_CONCEPT,
                    end_key="rx:b",
                    tenant_id=TENANT_A,
                    provenance=_sourced(),
                )
            ]
        )
        query, params = manager.queries[0]
        assert "MERGE (a)-[r:CONTRAINDICATED_WITH {tenant_id: row.tenant_id}]->(b)" in query
        assert params["rows"][0]["tenant_id"] == str(TENANT_A)  # type: ignore[index]

    async def test_neighbours_filters_on_the_edge_tenant(
        self, store, manager: _RecordingManager
    ) -> None:
        """Endpoint-only filtering exposed tenant-scoped edges between shared nodes.

        Both endpoints of such an edge have a null tenant, so filtering only the endpoints
        lets every tenant read it.
        """
        await store.neighbours(label=NodeLabel.RXNORM_CONCEPT, key="rx:a", tenant_id=TENANT_A)
        query, _ = manager.queries[0]
        assert "r.tenant_id IS NULL OR r.tenant_id = $tenant_id" in query

    async def test_full_text_search_never_receives_raw_user_text(
        self, store, manager: _RecordingManager
    ) -> None:
        """Lucene metacharacters in an ordinary clinical question crashed graph retrieval.

        ``queryNodes`` takes a Lucene *query string*; an unbalanced parenthesis or quote
        raises a parse error, which the pipeline then reports as a degraded strategy rather
        than an error — silently, and only in production.
        """
        hostile = 'CT (chest) Na+/K+ ratio "unclosed AND lisinopril~'
        await store.find_nodes(tenant_id=TENANT_A, text=hostile)
        _, params = manager.queries[0]
        sent = str(params["text"])
        assert not set(sent) & set(r"""()+/"~:*^[]{}\!""")
        assert "lisinopril" in sent


class TestEntityMatchRanking:
    """Regression: in-memory entry-point selection ignored match strength."""

    async def test_the_strongest_match_is_returned_first(self) -> None:
        """Neo4j orders by full-text score; taking the first `limit` in insertion order
        made the local store anchor traversal on different entities than production."""
        store = InMemoryGraphStore()
        await store.upsert_nodes(
            [
                GraphNode(
                    label=NodeLabel.RXNORM_CONCEPT,
                    key="rx:metformin",
                    properties={"display_text": "Metformin"},
                ),
                GraphNode(
                    label=NodeLabel.RXNORM_CONCEPT,
                    key="rx:lisinopril",
                    properties={"display_text": "Lisinopril hydrochlorothiazide"},
                ),
            ]
        )

        found = await store.find_nodes(
            tenant_id=TENANT_A,
            label=NodeLabel.RXNORM_CONCEPT,
            text="lisinopril hydrochlorothiazide dosing",
            limit=1,
        )
        assert [node.key for node in found] == ["rx:lisinopril"]

    async def test_short_tokens_do_not_match_everything(self) -> None:
        """Node keys are prefixed ("rx:..."), so a two-character token matched every
        concept in the ontology and turned entity linking into a random sample."""
        store = InMemoryGraphStore()
        await store.upsert_nodes(
            [
                GraphNode(
                    label=NodeLabel.RXNORM_CONCEPT,
                    key="rx:metformin",
                    properties={"display_text": "Metformin"},
                )
            ]
        )
        assert await store.find_nodes(tenant_id=TENANT_A, text="is it an rx or an ok") == []
