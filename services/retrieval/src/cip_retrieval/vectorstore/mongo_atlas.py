"""MongoDB Atlas Vector Search backend.

The production vector tier (ADR-0007). Uses the ``$vectorSearch`` aggregation stage, which
exists only on Atlas — a local ``mongod`` will reject the pipeline, which is why
:class:`~cip_retrieval.vectorstore.memory.InMemoryVectorStore` exists for development.

The single most important detail is that ``tenant_id`` goes in the ``filter`` clause of
``$vectorSearch``, not in a ``$match`` after it. Atlas applies that filter *inside* the
vector index while traversing candidates. A post-``$match`` would filter the returned
top-K, which is not the same thing: if another tenant's documents crowd this tenant's out
of the candidate pool, post-filtering returns an empty result for a query that should have
matched. That is the Phase 0 review's finding D5, and it is a correctness property, not an
optimisation.

``numCandidates`` controls the ANN candidate pool. Atlas requires it to exceed ``limit``;
too small and recall collapses, too large and latency climbs. The default multiplier below
follows MongoDB's guidance of roughly 10-20x the limit.
"""

from __future__ import annotations

import uuid
from typing import Any

from cip_core.errors import DependencyUnavailableError
from cip_core.logging import get_logger
from cip_retrieval.vectorstore.base import VectorMatch, VectorQuery, VectorRecord

__all__ = ["MongoAtlasVectorStore"]

_log = get_logger(__name__)

#: Multiplier applied to ``top_k`` when the caller does not specify ``num_candidates``.
_CANDIDATE_MULTIPLIER = 15

#: Atlas caps the candidate pool; exceeding it is an error rather than a clamp.
_MAX_CANDIDATES = 10_000


class MongoAtlasVectorStore:
    """Vector search backed by an Atlas ``$vectorSearch`` index.

    The index must exist before queries run — Atlas Search indexes are created through the
    Atlas API or UI, not through the driver. :meth:`index_definition` returns the exact
    definition this class expects so it can be applied by infrastructure automation and
    kept in version control rather than configured by hand.
    """

    def __init__(
        self,
        collection: Any,
        *,
        index_name: str = "chunk_embeddings_vector_index",
        path: str = "values",
    ) -> None:
        self._collection = collection
        self._index_name = index_name
        self._path = path

    @property
    def name(self) -> str:
        return "mongodb-atlas"

    @staticmethod
    def index_definition(dimensions: int) -> dict[str, Any]:
        """The Atlas Search index this store requires.

        Every field used as a query filter must be declared here as a ``filter`` type.
        Atlas silently ignores filters on undeclared fields — it does not error — so an
        omission here becomes a *tenant isolation failure* that looks like working code.
        That is the reason this definition lives beside the query builder rather than only
        in infrastructure config.
        """
        return {
            "name": "chunk_embeddings_vector_index",
            "type": "vectorSearch",
            "definition": {
                "fields": [
                    {
                        "type": "vector",
                        "path": "values",
                        "numDimensions": dimensions,
                        "similarity": "cosine",
                    },
                    {"type": "filter", "path": "tenant_id"},
                    {"type": "filter", "path": "model_key"},
                    {"type": "filter", "path": "patient_id"},
                    {"type": "filter", "path": "document_id"},
                    {"type": "filter", "path": "document_type"},
                    {"type": "filter", "path": "section_name"},
                ]
            },
        }

    async def upsert(self, records: list[VectorRecord]) -> int:
        """Insert or replace records by id.

        Uses ``bulk_write`` with per-record ``ReplaceOne(upsert=True)`` rather than
        ``insert_many``: re-indexing a document must replace its vectors, and an insert
        would accumulate duplicates that then compete with each other in search results.
        """
        if not records:
            return 0

        from pymongo import ReplaceOne

        operations = [
            ReplaceOne({"_id": record.id}, self._to_document(record), upsert=True)
            for record in records
        ]
        try:
            result = await self._collection.bulk_write(operations, ordered=False)
        except Exception as exc:
            raise DependencyUnavailableError(
                f"Vector upsert failed: {type(exc).__name__}", dependency="vector_store"
            ) from exc

        written = int(result.upserted_count) + int(result.modified_count)
        _log.debug("vectorstore.upserted", backend=self.name, count=written)
        return written

    async def search(self, query: VectorQuery) -> list[VectorMatch]:
        """Run an Atlas vector search with all filters pushed into the index."""
        pipeline = self._build_pipeline(query)
        try:
            cursor = await self._collection.aggregate(pipeline)
            documents = await cursor.to_list(length=query.top_k)
        except Exception as exc:
            raise DependencyUnavailableError(
                f"Vector search failed: {type(exc).__name__}", dependency="vector_store"
            ) from exc

        matches = [
            VectorMatch(record=self._from_document(document), score=float(document["score"]))
            for document in documents
        ]
        if query.min_score is not None:
            matches = [match for match in matches if match.score >= query.min_score]
        return matches

    def _build_pipeline(self, query: VectorQuery) -> list[dict[str, Any]]:
        """Build the aggregation pipeline.

        Separated from :meth:`search` so the pipeline — where a misplaced filter becomes a
        tenant leak — can be asserted on directly in tests without an Atlas cluster.
        """
        # tenant_id and model_key are non-negotiable: the first is isolation, the second is
        # correctness (vectors from another model are not comparable).
        conditions: list[dict[str, Any]] = [
            {"tenant_id": {"$eq": str(query.tenant_id)}},
            {"model_key": {"$eq": query.model_key}},
        ]
        if query.patient_id is not None:
            conditions.append({"patient_id": {"$eq": str(query.patient_id)}})
        if query.document_types:
            conditions.append({"document_type": {"$in": list(query.document_types)}})
        if query.section_names:
            conditions.append({"section_name": {"$in": list(query.section_names)}})
        if query.document_ids:
            conditions.append(
                {"document_id": {"$in": [str(value) for value in query.document_ids]}}
            )

        num_candidates = min(
            query.num_candidates or query.top_k * _CANDIDATE_MULTIPLIER, _MAX_CANDIDATES
        )

        return [
            {
                "$vectorSearch": {
                    "index": self._index_name,
                    "path": self._path,
                    "queryVector": list(query.values),
                    "numCandidates": num_candidates,
                    "limit": query.top_k,
                    "filter": {"$and": conditions},
                }
            },
            {
                "$addFields": {
                    # Atlas returns cosine similarity already mapped to [0, 1], matching
                    # the normalisation the in-memory store applies, so a `min_score`
                    # threshold means the same thing against either backend.
                    "score": {"$meta": "vectorSearchScore"}
                }
            },
        ]

    @staticmethod
    def _to_document(record: VectorRecord) -> dict[str, Any]:
        """Serialise a record. UUIDs become strings so Atlas filters compare them as scalars."""
        return {
            "_id": record.id,
            "tenant_id": str(record.tenant_id),
            "values": list(record.values),
            "model_key": record.model_key,
            "text": record.text,
            "document_id": str(record.document_id) if record.document_id else None,
            "patient_id": str(record.patient_id) if record.patient_id else None,
            "chunk_index": record.chunk_index,
            "section_name": record.section_name,
            "section_heading": record.section_heading,
            "page_start": record.page_start,
            "page_end": record.page_end,
            "document_type": record.document_type,
            "source_system": record.source_system,
            "effective_date": record.effective_date,
            "metadata": record.metadata,
        }

    @staticmethod
    def _from_document(document: dict[str, Any]) -> VectorRecord:
        return VectorRecord(
            id=str(document["_id"]),
            tenant_id=uuid.UUID(document["tenant_id"]),
            values=tuple(document.get("values", ())),
            model_key=document["model_key"],
            text=document.get("text", ""),
            document_id=uuid.UUID(document["document_id"]) if document.get("document_id") else None,
            patient_id=uuid.UUID(document["patient_id"]) if document.get("patient_id") else None,
            chunk_index=document.get("chunk_index"),
            section_name=document.get("section_name"),
            section_heading=document.get("section_heading"),
            page_start=document.get("page_start"),
            page_end=document.get("page_end"),
            document_type=document.get("document_type"),
            source_system=document.get("source_system"),
            effective_date=document.get("effective_date"),
            metadata=document.get("metadata", {}),
        )

    async def delete_document(self, document_id: uuid.UUID, *, tenant_id: uuid.UUID) -> int:
        result = await self._collection.delete_many(
            {"document_id": str(document_id), "tenant_id": str(tenant_id)}
        )
        return int(result.deleted_count)

    async def count(self, *, tenant_id: uuid.UUID) -> int:
        return int(await self._collection.count_documents({"tenant_id": str(tenant_id)}))

    async def health_check(self) -> dict[str, Any]:
        try:
            total = await self._collection.estimated_document_count()
        except Exception as exc:
            raise DependencyUnavailableError(
                f"Vector store health check failed: {type(exc).__name__}",
                dependency="vector_store",
            ) from exc
        return {"status": "ok", "backend": self.name, "index": self._index_name, "vectors": total}
