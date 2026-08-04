"""Retrievers: one per retrieval strategy, all behind a common protocol.

``VectorRetriever``  — semantic similarity over embeddings.
``KeywordRetriever`` — exact term/code/value matching via BM25.
``GraphRetriever``   — multi-hop relationships from the knowledge graph.

Each records its own rank and score on the candidates it returns; fusion combines them by
rank (see :mod:`cip_retrieval.fusion`).
"""

from cip_retrieval.retrievers.base import Retriever
from cip_retrieval.retrievers.graph import GraphRetriever
from cip_retrieval.retrievers.keyword import (
    BM25Index,
    BM25Options,
    IndexedDocument,
    tokenize_clinical,
)
from cip_retrieval.retrievers.keyword_retriever import KeywordRetriever
from cip_retrieval.retrievers.vector import VectorRetriever

__all__ = [
    "BM25Index",
    "BM25Options",
    "GraphRetriever",
    "IndexedDocument",
    "KeywordRetriever",
    "Retriever",
    "VectorRetriever",
    "tokenize_clinical",
]
