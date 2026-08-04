"""Query routing.

Different clinical questions are answered well by different retrievers, and routing decides
the mix:

* "what was the sodium on admission" — a **numeric lookup**. Dense vectors cannot separate
  141 from 151; BM25 can. Keyword-dominant.
* "what interacts with this patient's lisinopril" — a **relationship** question. The answer
  is not textually similar to the question; it is two graph edges away. Graph-dominant.
* "what is hyperkalemia" — a **definitional** question, answered by prose that resembles the
  question. Vector-dominant.

Two design choices are deliberate.

**Rules first, not a classifier.** Clinical question shapes are a small, stable set with
strong lexical markers, and a rule set is inspectable, testable, and free — where an LLM
classifier would add a network call and a failure mode to every query. The rules return a
confidence, so the pipeline can tell a firm match from a guess.

**Low confidence widens rather than narrows.** An uncertain route dispatches the broad
default bundle instead of committing to one strategy. Guessing narrowly and being wrong
costs the answer entirely; guessing broadly costs latency.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from cip_core.logging import get_logger
from cip_retrieval.domain import QueryIntent, RetrievalStrategy
from cip_retrieval.fusion import FusionWeights

__all__ = ["QueryRouter", "RoutingDecision", "RoutingRules"]

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    """The chosen intent, strategies, and weights for a query."""

    intent: QueryIntent
    confidence: float
    strategies: tuple[RetrievalStrategy, ...]
    weights: FusionWeights
    matched_signals: tuple[str, ...] = ()
    """Which markers fired. Recorded so a surprising route can be explained without
    re-deriving the classification by hand."""


@dataclass(frozen=True, slots=True)
class RoutingRules:
    """Weight profiles per intent.

    No profile zeroes every other strategy. A graph-dominant question still benefits from
    the vector hit the graph missed, and a corpus with no graph coverage yet must still
    return *something* rather than nothing.
    """

    factual_lookup: FusionWeights = FusionWeights(vector=0.6, keyword=2.0, graph=0.4)
    entity_relationship: FusionWeights = FusionWeights(vector=0.7, keyword=0.5, graph=2.0)
    definitional: FusionWeights = FusionWeights(vector=2.0, keyword=0.7, graph=0.6)
    narrative: FusionWeights = FusionWeights(vector=1.5, keyword=1.0, graph=0.5)
    thematic: FusionWeights = FusionWeights(vector=1.2, keyword=0.6, graph=1.5)
    default: FusionWeights = FusionWeights(vector=1.0, keyword=1.0, graph=1.0)

    def for_intent(self, intent: QueryIntent) -> FusionWeights:
        return {
            QueryIntent.FACTUAL_LOOKUP: self.factual_lookup,
            QueryIntent.ENTITY_RELATIONSHIP: self.entity_relationship,
            QueryIntent.DEFINITIONAL: self.definitional,
            QueryIntent.NARRATIVE: self.narrative,
            QueryIntent.THEMATIC: self.thematic,
            QueryIntent.UNKNOWN: self.default,
        }[intent]


#: Lexical markers per intent, each with a weight reflecting how strongly it discriminates.
#: "interacts with" almost only appears in relationship questions; "what" appears
#: everywhere, so it is weighted far lower.
_SIGNALS: dict[QueryIntent, tuple[tuple[re.Pattern[str], float, str], ...]] = {
    QueryIntent.FACTUAL_LOOKUP: (
        (re.compile(r"\b(?:what (?:was|were|is) the)\b", re.I), 1.5, "value_question"),
        (re.compile(r"\b(?:level|value|result|reading|count|dose|dosage)\b", re.I), 2.0, "measure"),
        (re.compile(r"\b\d+(?:\.\d+)?\s*(?:mg|mcg|ml|dl|mmol|meq|g|kg|mmhg)\b", re.I), 2.5, "unit"),
        (
            re.compile(
                r"\b(?:sodium|potassium|creatinine|troponin|hemoglobin|glucose|inr)\b", re.I
            ),
            2.0,
            "analyte",
        ),
        (
            re.compile(r"\b(?:on admission|at discharge|most recent|latest)\b", re.I),
            1.5,
            "temporal_anchor",
        ),
    ),
    QueryIntent.ENTITY_RELATIONSHIP: (
        (re.compile(r"\binteract(?:s|ion|ions)?\b", re.I), 3.0, "interaction"),
        (re.compile(r"\bcontraindicat", re.I), 3.0, "contraindication"),
        (re.compile(r"\b(?:side effect|adverse (?:event|reaction))\b", re.I), 2.5, "adverse_event"),
        (re.compile(r"\b(?:treats?|treated with|indicated for)\b", re.I), 2.0, "treats"),
        (re.compile(r"\b(?:cause[sd]?|leads? to|associated with|risk of)\b", re.I), 2.0, "causal"),
        (re.compile(r"\b(?:related to|connected to|linked to)\b", re.I), 1.5, "relational"),
    ),
    QueryIntent.DEFINITIONAL: (
        (re.compile(r"^\s*what (?:is|are)\b", re.I), 2.5, "what_is"),
        (re.compile(r"\b(?:define|definition of|meaning of)\b", re.I), 3.0, "define"),
        (re.compile(r"\bexplain\b", re.I), 2.0, "explain"),
        (re.compile(r"\bhow does .* work\b", re.I), 2.0, "mechanism"),
    ),
    QueryIntent.NARRATIVE: (
        (
            re.compile(r"\b(?:summar(?:y|ise|ize)|describe|overview|history of)\b", re.I),
            2.5,
            "summary",
        ),
        (re.compile(r"\b(?:hospital course|what happened|progress)\b", re.I), 2.5, "course"),
        (re.compile(r"\b(?:why was|why did)\b", re.I), 1.5, "reasoning"),
    ),
    QueryIntent.THEMATIC: (
        (
            re.compile(r"\b(?:across|trends?|patterns?|emerging|signals?)\b", re.I),
            2.5,
            "corpus_wide",
        ),
        (
            re.compile(r"\b(?:all patients|cohort|population|this quarter|overall)\b", re.I),
            2.5,
            "aggregate",
        ),
        (re.compile(r"\b(?:how many|how often|frequency of)\b", re.I), 2.0, "aggregate_count"),
    ),
}

#: Which retrievers to dispatch. All three run for every intent: weighting, not exclusion,
#: is how routing expresses preference. Skipping a strategy outright means a question the
#: router mis-classified loses its only good source, and because the retrievers run
#: concurrently the cost of running all three is the slowest one rather than their sum.
_ALL_STRATEGIES: tuple[RetrievalStrategy, ...] = (
    RetrievalStrategy.VECTOR,
    RetrievalStrategy.KEYWORD,
    RetrievalStrategy.GRAPH,
)

#: Below this the route is treated as a guess and the broad bundle is dispatched instead.
_MIN_CONFIDENCE = 0.35

#: Cap on how many distinct markers within one signal can contribute. Prevents a query
#: that repeats synonyms from letting a single signal outweigh every other intent.
_MAX_MARKERS_PER_SIGNAL = 2


class QueryRouter:
    """Classifies a query and selects strategies and weights."""

    def __init__(self, rules: RoutingRules | None = None) -> None:
        self._rules = rules or RoutingRules()

    def route(self, text: str, *, declared_intent: QueryIntent | None = None) -> RoutingDecision:
        """Decide how to retrieve for ``text``.

        A caller-declared intent is honoured without classification — an integration that
        knows it is issuing a lab lookup has better information than any classifier, and
        second-guessing it would be a regression from its point of view.
        """
        if declared_intent is not None:
            return RoutingDecision(
                intent=declared_intent,
                confidence=1.0,
                strategies=_ALL_STRATEGIES,
                weights=self._rules.for_intent(declared_intent),
                matched_signals=("declared",),
            )

        scores: dict[QueryIntent, float] = {}
        matched: dict[QueryIntent, list[str]] = {}

        for intent, signals in _SIGNALS.items():
            total = 0.0
            for pattern, weight, name in signals:
                # Count *distinct markers* matched, not merely whether the pattern fired.
                # Several related markers share one alternation, so a boolean check scores
                # "emerging safety signals across trials" (three thematic markers) the same
                # as a query hitting one — which let a weak match from another intent tie
                # with it and win on dict order.
                distinct = len({match.lower() for match in pattern.findall(text)})
                if distinct == 0:
                    continue
                total += weight * min(distinct, _MAX_MARKERS_PER_SIGNAL)
                matched.setdefault(intent, []).append(name)
            if total > 0.0:
                scores[intent] = total

        if not scores:
            return self._fallback(text, reason="no_signal")

        ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
        best_intent, best_score = ranked[0]
        runner_up = ranked[1][1] if len(ranked) > 1 else 0.0

        # Confidence comes from the *margin*, not the absolute score: a query matching two
        # intents equally strongly is genuinely ambiguous however many markers it hit.
        margin = (best_score - runner_up) / best_score
        confidence = round(min(1.0, 0.4 + 0.6 * margin), 4)

        if confidence < _MIN_CONFIDENCE:
            return self._fallback(text, reason="ambiguous")

        decision = RoutingDecision(
            intent=best_intent,
            confidence=confidence,
            strategies=_ALL_STRATEGIES,
            weights=self._rules.for_intent(best_intent),
            matched_signals=tuple(matched.get(best_intent, ())),
        )
        _log.debug(
            "routing.decided",
            intent=str(decision.intent),
            confidence=decision.confidence,
            signals=list(decision.matched_signals),
        )
        return decision

    def _fallback(self, text: str, *, reason: str) -> RoutingDecision:
        """Dispatch everything when the intent is unclear.

        Widening rather than guessing: an unrecognised question is far more likely to be
        answered by *some* strategy than by the one a coin-flip picked.
        """
        _log.debug("routing.fallback", reason=reason, query_length=len(text))
        return RoutingDecision(
            intent=QueryIntent.UNKNOWN,
            confidence=0.0,
            strategies=_ALL_STRATEGIES,
            weights=self._rules.default,
            matched_signals=(reason,),
        )
