"""Evaluation: retrieval metrics, grounding checks, and the harness that runs them.

Every retrieval configuration is scored on the same labelled set, so a change to embeddings,
fusion weights, or reranking is a measurement rather than an impression.
"""

from cip_retrieval.evaluation.grounding import (
    GroundingReport,
    answer_relevance,
    assess_grounding,
    citation_accuracy,
    extract_citations,
    groundedness,
    numeric_consistency,
)
from cip_retrieval.evaluation.harness import (
    EvalCase,
    EvalReport,
    EvalResult,
    RetrievalEvaluator,
)
from cip_retrieval.evaluation.metrics import (
    average_precision,
    context_precision,
    context_recall,
    hit_rate,
    mean_reciprocal_rank,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
)

__all__ = [
    "EvalCase",
    "EvalReport",
    "EvalResult",
    "GroundingReport",
    "RetrievalEvaluator",
    "answer_relevance",
    "assess_grounding",
    "average_precision",
    "citation_accuracy",
    "context_precision",
    "context_recall",
    "extract_citations",
    "groundedness",
    "hit_rate",
    "mean_reciprocal_rank",
    "ndcg_at_k",
    "numeric_consistency",
    "precision_at_k",
    "recall_at_k",
    "reciprocal_rank",
]
