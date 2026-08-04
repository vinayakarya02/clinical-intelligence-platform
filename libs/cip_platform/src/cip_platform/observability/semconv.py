"""OpenTelemetry GenAI semantic conventions.

The standard `gen_ai.*` attribute and instrument names, centralised so a convention bump is
one diff rather than a search across the codebase
(docs/design/adr-0016-otel-genai-conventions.md).

Using the standard vocabulary rather than our own is the whole point: telemetry emitted with
these names is understood by any conformant backend — Grafana, Datadog, a vendor LLM
observability product — without a translation layer. The dashboards in this repository are one
consumer of it, not the only possible one.

The conventions are **experimental** as of the targeted version below and will change. Where
no standard name exists for something this platform measures — per-request USD cost, per-stage
pipeline durations, cache hit rates — a clearly-namespaced local name is used and marked
:data:`LOCAL_EXTENSIONS`, so a future reader can tell what is standard from what we invented.
"""

from __future__ import annotations

from typing import Final

__all__ = [
    "GEN_AI",
    "LOCAL",
    "LOCAL_EXTENSIONS",
    "SEMCONV_VERSION",
    "Instruments",
    "OperationName",
]

#: The semantic-convention version these names target. Recorded because the conventions are
#: experimental: a reader comparing this module against a newer spec needs to know which one it
#: was written against.
SEMCONV_VERSION: Final[str] = "1.37"


class OperationName:
    """Well-known values for ``gen_ai.operation.name``."""

    CHAT: Final[str] = "chat"
    EMBEDDINGS: Final[str] = "embeddings"
    RETRIEVAL: Final[str] = "retrieval"
    EXECUTE_TOOL: Final[str] = "execute_tool"
    INVOKE_AGENT: Final[str] = "invoke_agent"
    INVOKE_WORKFLOW: Final[str] = "invoke_workflow"


class GEN_AI:  # noqa: N801 - mirrors the specification's namespace, not Python convention
    """Standard ``gen_ai.*`` attribute names."""

    OPERATION_NAME: Final[str] = "gen_ai.operation.name"
    PROVIDER_NAME: Final[str] = "gen_ai.provider.name"

    REQUEST_MODEL: Final[str] = "gen_ai.request.model"
    REQUEST_TEMPERATURE: Final[str] = "gen_ai.request.temperature"
    REQUEST_MAX_TOKENS: Final[str] = "gen_ai.request.max_tokens"
    RESPONSE_MODEL: Final[str] = "gen_ai.response.model"
    RESPONSE_ID: Final[str] = "gen_ai.response.id"
    RESPONSE_FINISH_REASONS: Final[str] = "gen_ai.response.finish_reasons"

    USAGE_INPUT_TOKENS: Final[str] = "gen_ai.usage.input_tokens"
    USAGE_OUTPUT_TOKENS: Final[str] = "gen_ai.usage.output_tokens"
    TOKEN_TYPE: Final[str] = "gen_ai.token.type"

    AGENT_NAME: Final[str] = "gen_ai.agent.name"
    AGENT_ID: Final[str] = "gen_ai.agent.id"
    CONVERSATION_ID: Final[str] = "gen_ai.conversation.id"
    WORKFLOW_NAME: Final[str] = "gen_ai.workflow.name"

    TOOL_NAME: Final[str] = "gen_ai.tool.name"
    TOOL_CALL_ID: Final[str] = "gen_ai.tool.call.id"
    TOOL_TYPE: Final[str] = "gen_ai.tool.type"

    EMBEDDINGS_DIMENSION_COUNT: Final[str] = "gen_ai.embeddings.dimension.count"

    #: Quality scores. These are why the standard fits this platform so well: Phase 3 already
    #: produces confidence, hallucination rate, and groundedness, and these attributes are
    #: exactly the shape for reporting them.
    EVALUATION_NAME: Final[str] = "gen_ai.evaluation.name"
    EVALUATION_SCORE_VALUE: Final[str] = "gen_ai.evaluation.score.value"
    EVALUATION_SCORE_LABEL: Final[str] = "gen_ai.evaluation.score.label"
    EVALUATION_EXPLANATION: Final[str] = "gen_ai.evaluation.explanation"

    PROMPT_NAME: Final[str] = "gen_ai.prompt.name"

    #: Content attributes. Never set by this platform in a deployed environment — the content
    #: is PHI, and attaching it to a span exports it to the telemetry backend.
    INPUT_MESSAGES: Final[str] = "gen_ai.input.messages"
    OUTPUT_MESSAGES: Final[str] = "gen_ai.output.messages"
    SYSTEM_INSTRUCTIONS: Final[str] = "gen_ai.system_instructions"


class LOCAL:
    """Local extensions, for concepts the conventions do not cover.

    Namespaced ``cip.*`` so they are unmistakably ours. Each exists because the standard has
    no equivalent, not because the standard was inconvenient.
    """

    COST_USD: Final[str] = "cip.cost.usd"
    TENANT_ID: Final[str] = "cip.tenant.id"
    CORRELATION_ID: Final[str] = "cip.correlation.id"
    PIPELINE_STAGE: Final[str] = "cip.pipeline.stage"
    RESPONSE_MODE: Final[str] = "cip.response.mode"
    CACHE_DOMAIN: Final[str] = "cip.cache.domain"
    CACHE_RESULT: Final[str] = "cip.cache.result"
    EVIDENCE_KIND: Final[str] = "cip.evidence.kind"
    CLAIM_COUNT: Final[str] = "cip.claim.count"
    DEGRADED_STRATEGY: Final[str] = "cip.retrieval.degraded"
    JOB_KIND: Final[str] = "cip.job.kind"


#: Every local extension, so a reviewer can audit the non-standard surface in one place and a
#: future convention release can be checked against it for things that became standard.
LOCAL_EXTENSIONS: Final[frozenset[str]] = frozenset(
    {
        LOCAL.COST_USD,
        LOCAL.TENANT_ID,
        LOCAL.CORRELATION_ID,
        LOCAL.PIPELINE_STAGE,
        LOCAL.RESPONSE_MODE,
        LOCAL.CACHE_DOMAIN,
        LOCAL.CACHE_RESULT,
        LOCAL.EVIDENCE_KIND,
        LOCAL.CLAIM_COUNT,
        LOCAL.DEGRADED_STRATEGY,
        LOCAL.JOB_KIND,
    }
)


class Instruments:
    """Metric instrument names.

    The ``gen_ai.client.*`` names are standard. The ``cip_*`` names are local, and use
    Prometheus naming (``_seconds``, ``_total``) because that is what the exposition format
    and every alerting rule written against it expect.
    """

    #: Standard.
    OPERATION_DURATION: Final[str] = "gen_ai.client.operation.duration"
    TOKEN_USAGE: Final[str] = "gen_ai.client.token.usage"

    #: Local.
    COST_USD_TOTAL: Final[str] = "cip_llm_cost_usd_total"
    REQUEST_DURATION: Final[str] = "cip_request_duration_seconds"
    STAGE_DURATION: Final[str] = "cip_pipeline_stage_duration_seconds"
    RETRIEVAL_DURATION: Final[str] = "cip_retrieval_duration_seconds"
    GRAPH_TRAVERSAL_DURATION: Final[str] = "cip_graph_traversal_duration_seconds"
    TOOL_DURATION: Final[str] = "cip_tool_duration_seconds"
    CACHE_OPERATIONS: Final[str] = "cip_cache_operations_total"
    ANSWERS_TOTAL: Final[str] = "cip_answers_total"
    EVALUATION_SCORE: Final[str] = "cip_evaluation_score"
    TASK_DURATION: Final[str] = "cip_task_duration_seconds"
    TASK_TOTAL: Final[str] = "cip_tasks_total"
    RATE_LIMIT_REJECTIONS: Final[str] = "cip_rate_limit_rejections_total"
    BUDGET_REJECTIONS: Final[str] = "cip_budget_rejections_total"
