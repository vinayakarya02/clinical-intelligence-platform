"""Vector storage and similarity search.

``MongoAtlasVectorStore`` is production (ADR-0007); ``InMemoryVectorStore`` is exact search
for development, CI, and small tenants. Both implement :class:`VectorStore` with identical
filter and threshold semantics, so retrieval behaves the same against either.

Adding Pinecone/Weaviate/Milvus means implementing the protocol — no retrieval code
changes. The interface is justified today by the local/production split, not by that
hypothetical.
"""

from cip_retrieval.vectorstore.base import VectorMatch, VectorQuery, VectorRecord, VectorStore
from cip_retrieval.vectorstore.memory import (
    InMemoryVectorStore,
    cosine_similarity,
    to_unit_interval,
)
from cip_retrieval.vectorstore.mongo_atlas import MongoAtlasVectorStore

__all__ = [
    "InMemoryVectorStore",
    "MongoAtlasVectorStore",
    "VectorMatch",
    "VectorQuery",
    "VectorRecord",
    "VectorStore",
    "cosine_similarity",
    "to_unit_interval",
]
