"""Evidence aggregation and claim construction."""

from cip_copilot.reasoning.aggregator import (
    AggregationResult,
    aggregate_evidence,
    build_claims,
    evidence_recency,
    extract_numbers,
)

__all__ = [
    "AggregationResult",
    "aggregate_evidence",
    "build_claims",
    "evidence_recency",
    "extract_numbers",
]
