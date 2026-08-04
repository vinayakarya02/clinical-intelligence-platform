"""AI observability: the metric set, and the observer that fills it from an answer.

The key design point is that this module *reads* a finished Phase 3 ``Answer`` and emits
telemetry. The copilot does not instrument itself, does not import the platform, and does not
know this exists (docs/design/adr-0013-platform-library-boundary.md). Everything the phase
asks to track — latency, retrieval time, graph traversal, tool usage, hallucination rate,
confidence, abstention, tokens, cost, cache hit rate — is already on the answer or its trace,
so observation is a projection rather than an instrumentation campaign.

Quality scores are emitted as ``gen_ai.evaluation.*`` rather than bespoke gauges, because that
is exactly what those attributes are for and it makes them legible to any conformant backend.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from cip_core.logging import get_logger
from cip_platform.observability.metrics import MetricRegistry
from cip_platform.observability.semconv import LOCAL, Instruments

__all__ = ["AIMetrics", "AnswerObserver"]

_log = get_logger(__name__)

#: Token-usage buckets. Wider than the latency buckets because prompt sizes span three orders
#: of magnitude between a one-line question and a packed clinical context.
_TOKEN_BUCKETS: tuple[float, ...] = (
    50,
    100,
    250,
    500,
    1000,
    2000,
    4000,
    8000,
    16000,
    32000,
    64000,
)

#: Score buckets over [0, 1] at 0.1 resolution. Finer would be false precision on a confidence
#: number derived from six weighted components.
_SCORE_BUCKETS: tuple[float, ...] = tuple(round(0.1 * i, 1) for i in range(11))


@dataclass(frozen=True, slots=True)
class AIMetrics:
    """Every instrument this platform exposes.

    Constructed once and passed around. Labels are deliberately low-cardinality — tenant,
    operation, model, status — because a per-request label is how a monitoring stack dies
    (see the cardinality guard in :mod:`cip_platform.observability.metrics`).
    """

    registry: MetricRegistry

    @classmethod
    def build(cls, registry: MetricRegistry) -> AIMetrics:
        metrics = cls(registry=registry)
        metrics._declare()
        return metrics

    def _declare(self) -> None:
        r = self.registry
        r.histogram(
            Instruments.OPERATION_DURATION,
            "Duration of a GenAI operation, in seconds.",
            ("gen_ai_operation_name", "gen_ai_provider_name", "gen_ai_request_model"),
        )
        r.histogram(
            Instruments.TOKEN_USAGE,
            "Tokens used by a GenAI operation.",
            ("gen_ai_operation_name", "gen_ai_token_type", "gen_ai_request_model"),
            buckets=_TOKEN_BUCKETS,
        )
        r.counter(
            Instruments.COST_USD_TOTAL,
            "Estimated provider spend in USD. Local extension: no standard instrument exists.",
            ("tenant", "gen_ai_request_model"),
        )
        r.histogram(
            Instruments.REQUEST_DURATION,
            "End-to-end request duration, in seconds.",
            ("route", "status"),
        )
        r.histogram(
            Instruments.STAGE_DURATION,
            "Copilot pipeline stage duration, in seconds.",
            ("stage",),
        )
        r.histogram(
            Instruments.RETRIEVAL_DURATION,
            "Retrieval duration, in seconds.",
            ("tenant",),
        )
        r.histogram(
            Instruments.GRAPH_TRAVERSAL_DURATION,
            "Graph traversal duration, in seconds.",
            ("tenant",),
        )
        r.histogram(
            Instruments.TOOL_DURATION,
            "Tool execution duration, in seconds.",
            ("gen_ai_tool_name", "status"),
        )
        r.counter(
            Instruments.CACHE_OPERATIONS,
            "Cache operations by domain and result.",
            ("cache_domain", "cache_result"),
        )
        r.counter(
            Instruments.ANSWERS_TOTAL,
            "Answers produced, by response mode.",
            ("tenant", "response_mode"),
        )
        r.histogram(
            Instruments.EVALUATION_SCORE,
            "Quality scores, per gen_ai.evaluation.name.",
            ("gen_ai_evaluation_name",),
            buckets=_SCORE_BUCKETS,
        )
        r.counter(Instruments.TASK_TOTAL, "Background tasks.", ("job_kind", "status"))
        r.histogram(
            Instruments.TASK_DURATION, "Background task duration, in seconds.", ("job_kind",)
        )
        r.counter(
            Instruments.RATE_LIMIT_REJECTIONS, "Requests refused by a rate limit.", ("scope",)
        )
        r.counter(Instruments.BUDGET_REJECTIONS, "Requests refused by a spend budget.", ("tenant",))

    # ---- recording helpers -----------------------------------------------------------

    def record_model_call(
        self,
        *,
        operation: str,
        provider: str,
        model: str,
        duration_seconds: float,
        input_tokens: int,
        output_tokens: int,
        tenant: str,
        cost_usd: float = 0.0,
    ) -> None:
        """One model call, in standard-conventions form."""
        self._histogram(Instruments.OPERATION_DURATION).observe(
            duration_seconds,
            gen_ai_operation_name=operation,
            gen_ai_provider_name=provider,
            gen_ai_request_model=model,
        )
        tokens = self._histogram(Instruments.TOKEN_USAGE)
        tokens.observe(
            input_tokens,
            gen_ai_operation_name=operation,
            gen_ai_token_type="input",
            gen_ai_request_model=model,
        )
        tokens.observe(
            output_tokens,
            gen_ai_operation_name=operation,
            gen_ai_token_type="output",
            gen_ai_request_model=model,
        )
        if cost_usd:
            self._counter(Instruments.COST_USD_TOTAL).inc(
                cost_usd, tenant=tenant, gen_ai_request_model=model
            )

    def record_cache(self, *, domain: str, hit: bool) -> None:
        self._counter(Instruments.CACHE_OPERATIONS).inc(
            cache_domain=domain, cache_result="hit" if hit else "miss"
        )

    def record_task(self, *, job_kind: str, status: str, duration_seconds: float) -> None:
        self._counter(Instruments.TASK_TOTAL).inc(job_kind=job_kind, status=status)
        self._histogram(Instruments.TASK_DURATION).observe(duration_seconds, job_kind=job_kind)

    def record_evaluation(self, *, name: str, score: float) -> None:
        """A quality score, under ``gen_ai.evaluation.name``."""
        self._histogram(Instruments.EVALUATION_SCORE).observe(score, gen_ai_evaluation_name=name)

    def record_rate_limit(self, *, scope: str) -> None:
        self._counter(Instruments.RATE_LIMIT_REJECTIONS).inc(scope=scope)

    def record_budget_rejection(self, *, tenant: str) -> None:
        self._counter(Instruments.BUDGET_REJECTIONS).inc(tenant=tenant)

    def record_request(self, *, route: str, status: str, duration_seconds: float) -> None:
        self._histogram(Instruments.REQUEST_DURATION).observe(
            duration_seconds, route=route, status=status
        )

    def _counter(self, name: str) -> Any:
        return self.registry.counter(name, "", self._labels(name))

    def _histogram(self, name: str) -> Any:
        return self.registry.histogram(name, "", self._labels(name))

    def _labels(self, name: str) -> tuple[str, ...]:
        """Label names for an already-declared instrument.

        Re-declaration returns the existing series, and the registry refuses a mismatched
        shape — so this looks the labels up rather than repeating them at each call site,
        where they would eventually diverge.
        """
        return self.registry._series[name].label_names


class AnswerObserver:
    """Turns a finished copilot answer into telemetry.

    Reads only the public surface of an ``Answer``: mode, confidence breakdown, trace,
    usage, and cited evidence. Nothing here imports the copilot — the answer is passed in —
    which is what keeps the platform below the application in the dependency order.
    """

    def __init__(self, metrics: AIMetrics, *, provider: str = "local", model: str = "extractive"):
        self._metrics = metrics
        self._provider = provider
        self._model = model

    def observe(self, answer: Any, *, tenant: str) -> None:
        """Emit every instrument this answer supports."""
        mode = str(getattr(answer, "mode", "unknown"))
        self._metrics._counter(Instruments.ANSWERS_TOTAL).inc(tenant=tenant, response_mode=mode)

        trace = tuple(getattr(answer, "trace", ()))
        total_seconds = sum(record.duration_ms for record in trace) / 1000.0
        for record in trace:
            self._metrics._histogram(Instruments.STAGE_DURATION).observe(
                record.duration_ms / 1000.0, stage=record.stage
            )

        usage = getattr(answer, "usage", None)
        if usage is not None and usage.calls:
            self._metrics.record_model_call(
                operation="chat",
                provider=self._provider,
                model=self._model,
                duration_seconds=total_seconds,
                input_tokens=usage.prompt_tokens,
                output_tokens=usage.completion_tokens,
                tenant=tenant,
            )

        confidence = getattr(answer, "confidence", None)
        if confidence is not None:
            self._metrics.record_evaluation(name="confidence", score=confidence.score)
            for component, value in confidence.as_dict().items():
                self._metrics.record_evaluation(name=component, score=value)

        # Hallucination rate and abstention come from the trace and the mode rather than from
        # a separate evaluation pass, because Phase 3 already computes both.
        for record in trace:
            if record.stage == "reflect":
                rate = record.details.get("hallucination_rate")
                if rate is not None:
                    self._metrics.record_evaluation(name="hallucination_rate", score=float(rate))
        self._metrics.record_evaluation(name="abstained", score=0.0 if mode == "answer" else 1.0)

        _log.debug(
            "observability.answer_observed",
            mode=mode,
            stages=len(trace),
            **{LOCAL.TENANT_ID.replace(".", "_"): tenant},
        )
