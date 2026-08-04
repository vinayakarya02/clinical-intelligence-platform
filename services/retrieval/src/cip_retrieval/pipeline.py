"""Retrieval pipeline.

Orchestrates route → retrieve (concurrently) → fuse → rerank → assemble → prompt.

Two properties are load-bearing.

**Retrievers run concurrently.** The latency budget in
docs/architecture/02-rag-hybrid-retrieval.md §5 is only achievable if total time is the
slowest strategy rather than their sum. A slow or failing retriever must also not take the
whole query down — each is isolated, and a failure degrades the result instead of erasing
it, which is the right trade when the alternative is answering nothing.

**The no-evidence gate is structural.** When nothing survives retrieval and filtering, the
pipeline renders the ``no_evidence`` prompt and never presents an answerable one. A model
handed an empty context answers from parametric memory, which is the single most common
hallucination mode in clinical RAG (§2.4) — and the grounding checker cannot catch it after
the fact, because there was never any context to check the answer against.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from cip_core.logging import get_logger
from cip_core.tenancy import TenantContext
from cip_retrieval.context import AssembledContext, ContextBudget, ContextBuilder
from cip_retrieval.domain import (
    RetrievalCandidate,
    RetrievalQuery,
    RetrievalStrategy,
    RetrievalTrace,
)
from cip_retrieval.fusion import reciprocal_rank_fusion
from cip_retrieval.prompts.registry import PromptRegistry, RenderedPrompt
from cip_retrieval.reranking import FeatureReranker, Reranker
from cip_retrieval.retrievers.base import Retriever
from cip_retrieval.routing import QueryRouter

__all__ = ["RetrievalPipeline", "RetrievalResponse"]

_log = get_logger(__name__)

#: How much deeper than ``top_k`` each strategy searches. Fusion needs a wider pool than
#: the caller wants: an item ranked 15th by vectors and 2nd by keywords should still
#: surface, and it cannot if each strategy only returned its own top 10.
_CANDIDATE_DEPTH_MULTIPLIER = 3


@dataclass(frozen=True, slots=True)
class RetrievalResponse:
    """Everything one retrieval produced."""

    candidates: tuple[RetrievalCandidate, ...]
    context: AssembledContext
    prompt: RenderedPrompt
    trace: RetrievalTrace
    degraded_strategies: tuple[str, ...] = field(default_factory=tuple)
    """Strategies that failed. Surfaced rather than swallowed: an answer assembled without
    the graph is a different answer, and the caller is entitled to know."""

    @property
    def has_evidence(self) -> bool:
        return not self.context.is_empty


class RetrievalPipeline:
    """End-to-end hybrid retrieval."""

    def __init__(
        self,
        *,
        retrievers: dict[RetrievalStrategy, Retriever],
        router: QueryRouter | None = None,
        reranker: Reranker | None = None,
        context_builder: ContextBuilder | None = None,
        prompts: PromptRegistry | None = None,
        budget: ContextBudget | None = None,
    ) -> None:
        if not retrievers:
            raise ValueError("At least one retriever is required")
        self._retrievers = retrievers
        self._router = router or QueryRouter()
        self._reranker = reranker or FeatureReranker()
        self._context = context_builder or ContextBuilder()
        self._prompts = prompts or PromptRegistry()
        self._budget = budget or ContextBudget()

    async def retrieve(self, query: RetrievalQuery, *, context: TenantContext) -> RetrievalResponse:
        """Run the full pipeline for one query."""
        context.require_scope("documents:read")
        context.require_tenant(query.tenant_id)

        timings: dict[str, float] = {}

        started = time.perf_counter()
        decision = self._router.route(query.text, declared_intent=query.intent)
        timings["route"] = (time.perf_counter() - started) * 1000

        depth = query.top_k * _CANDIDATE_DEPTH_MULTIPLIER

        started = time.perf_counter()
        per_strategy, degraded = await self._gather(query, decision.strategies, depth)
        timings["retrieve"] = (time.perf_counter() - started) * 1000

        started = time.perf_counter()
        fused = reciprocal_rank_fusion(per_strategy, weights=decision.weights)
        timings["fuse"] = (time.perf_counter() - started) * 1000

        # The post-retrieval re-check required by docs/architecture/02-rag-hybrid-retrieval.md
        # §2.2. Every store filters by tenant already; this is the independent second check,
        # placed here because this is the one point every candidate from every strategy
        # passes through. A store-level filter that regresses — a dropped Atlas index filter,
        # a Cypher predicate lost in a refactor — leaks PHI silently, and silence is the
        # problem: a mismatch here is logged and dropped rather than answered with.
        authorised, rejected = self._enforce_tenant(fused, query)
        if rejected:
            _log.error(
                "retrieval.cross_tenant_candidates_dropped",
                expected_tenant=str(query.tenant_id),
                dropped=rejected,
                strategies=[str(s) for s in per_strategy],
            )

        started = time.perf_counter()
        floored, below_threshold = self._apply_floor(authorised, query.min_score)
        reranked = await self._reranker.rerank(query, floored, limit=query.top_k)
        timings["rerank"] = (time.perf_counter() - started) * 1000

        trace = RetrievalTrace(
            intent=decision.intent,
            intent_confidence=decision.confidence,
            strategies_dispatched=decision.strategies,
            weights=decision.weights.as_dict(),
            candidates_per_strategy={
                str(strategy): len(items) for strategy, items in per_strategy.items()
            },
            fused_count=len(fused),
            reranked_count=len(reranked),
            returned_count=len(reranked),
            filtered_by_acl=rejected,
            filtered_by_threshold=below_threshold,
            stage_durations_ms=timings,
            notes=tuple(f"degraded:{name}" for name in degraded),
        )

        started = time.perf_counter()
        assembled = self._context.build(list(reranked), budget=self._budget, trace=trace)
        timings["assemble"] = (time.perf_counter() - started) * 1000

        started = time.perf_counter()
        prompt = self._render_prompt(query, assembled)
        timings["prompt"] = (time.perf_counter() - started) * 1000

        _log.info(
            "retrieval.completed",
            intent=str(decision.intent),
            returned=len(reranked),
            context_blocks=len(assembled.blocks),
            graph_evidence=len(assembled.graph_evidence),
            degraded=list(degraded),
            duration_ms=round(sum(timings.values()), 2),
        )

        return RetrievalResponse(
            candidates=tuple(reranked),
            context=assembled,
            prompt=prompt,
            trace=trace,
            degraded_strategies=tuple(degraded),
        )

    @staticmethod
    def _enforce_tenant(
        candidates: list[RetrievalCandidate], query: RetrievalQuery
    ) -> tuple[list[RetrievalCandidate], int]:
        """Drop any candidate not belonging to the querying tenant.

        Returns the surviving candidates and the number rejected. Dropping rather than
        raising is deliberate: one mis-scoped candidate must not deny a clinician an
        otherwise correct answer, and the count reaches the trace and the error log either
        way. A non-zero count is always a bug in a store filter, never a normal outcome.
        """
        authorised = [c for c in candidates if c.tenant_id == query.tenant_id]
        return authorised, len(candidates) - len(authorised)

    @staticmethod
    def _apply_floor(
        candidates: list[RetrievalCandidate], min_score: float | None
    ) -> tuple[list[RetrievalCandidate], int]:
        """Apply the caller's relevance floor uniformly across strategies.

        ``min_score`` was previously honoured only by the vector store, because only it has
        a naturally bounded score. That made the parameter actively harmful rather than
        merely incomplete: weak keyword and graph hits survived a filter the vector hits did
        not, and because fusion consumes *ranks*, those survivors were promoted into the
        space the filtered vector hits vacated.

        The floor is applied to each strategy's own recorded score, so every strategy is
        held to the same bar on its own scale, and it runs after fusion so a candidate that
        clears the bar on any strategy is kept.
        """
        if min_score is None:
            return candidates, 0
        kept = [c for c in candidates if any(score >= min_score for score in c.scores.values())]
        return kept, len(candidates) - len(kept)

    async def _gather(
        self,
        query: RetrievalQuery,
        strategies: tuple[RetrievalStrategy, ...],
        depth: int,
    ) -> tuple[dict[RetrievalStrategy, list[RetrievalCandidate]], list[str]]:
        """Run the selected retrievers concurrently, isolating failures."""
        active = [s for s in strategies if s in self._retrievers]
        if not active:
            return {}, []

        async def _run(strategy: RetrievalStrategy) -> list[RetrievalCandidate]:
            return await self._retrievers[strategy].retrieve(query, limit=depth)

        outcomes = await asyncio.gather(
            *(_run(strategy) for strategy in active), return_exceptions=True
        )

        results: dict[RetrievalStrategy, list[RetrievalCandidate]] = {}
        degraded: list[str] = []
        for strategy, outcome in zip(active, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                # Degrade rather than fail: two working strategies still produce a useful
                # answer, and an outage in one store should not black out retrieval.
                degraded.append(str(strategy))
                _log.warning(
                    "retrieval.strategy_failed",
                    strategy=str(strategy),
                    error=type(outcome).__name__,
                )
                continue
            results[strategy] = outcome
        return results, degraded

    def _render_prompt(self, query: RetrievalQuery, assembled: AssembledContext) -> RenderedPrompt:
        """Render the answering prompt, or the no-evidence prompt when nothing was found."""
        if assembled.is_empty:
            return self._prompts.compose(task="no_evidence", variables={"question": query.text})

        graph_section = ""
        if assembled.graph_evidence:
            fragment = self._prompts.get("graph_evidence_section")
            graph_section = fragment.render({"graph_evidence": assembled.render_graph_evidence()})

        return self._prompts.compose(
            task="answer_question",
            variables={
                "question": query.text,
                "evidence": assembled.render_evidence(),
                "graph_evidence_section": graph_section,
            },
        )
