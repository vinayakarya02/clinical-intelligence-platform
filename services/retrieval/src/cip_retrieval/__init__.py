"""``cip_retrieval`` — Phase 2 retrieval and reasoning engine.

Turns the chunks Phase 1 persisted into an intelligent retrieval system: embeddings, vector
and keyword and graph retrieval, RRF fusion, reranking, context assembly, and prompt
composition — with an evaluation framework so every part of it is measurable.

Scope boundary: this phase stops at a rendered prompt. Calling a model, managing
conversation state, and serving a chat interface are Phase 3.
"""

from cip_retrieval.context import AssembledContext, ContextBudget, ContextBuilder
from cip_retrieval.domain import (
    GraphEvidence,
    QueryIntent,
    RetrievalCandidate,
    RetrievalQuery,
    RetrievalStrategy,
    RetrievalTrace,
    SourceKind,
)
from cip_retrieval.fusion import FusionWeights, reciprocal_rank_fusion
from cip_retrieval.pipeline import RetrievalPipeline, RetrievalResponse
from cip_retrieval.reranking import FeatureReranker, Reranker, RerankWeights
from cip_retrieval.routing import QueryRouter, RoutingDecision, RoutingRules

__all__ = [
    "AssembledContext",
    "ContextBudget",
    "ContextBuilder",
    "FeatureReranker",
    "FusionWeights",
    "GraphEvidence",
    "QueryIntent",
    "QueryRouter",
    "RerankWeights",
    "Reranker",
    "RetrievalCandidate",
    "RetrievalPipeline",
    "RetrievalQuery",
    "RetrievalResponse",
    "RetrievalStrategy",
    "RetrievalTrace",
    "RoutingDecision",
    "RoutingRules",
    "SourceKind",
    "__version__",
    "reciprocal_rank_fusion",
]

__version__ = "0.2.0"
