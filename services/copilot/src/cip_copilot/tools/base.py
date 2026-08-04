"""Tool contract and registry.

A tool is a named capability with a JSON-Schema argument contract, a declared PHI class, and
a declared side-effect class. The registry enforces all three, so no individual tool has to
defend itself against malformed arguments or an under-scoped caller — that logic exists once
and is tested once.

Three enforcement points matter, and each closes a failure that would otherwise be invisible:

**Arguments are validated before execution.** A tool that validates its own inputs validates
them differently from its neighbours, and the one that forgets is discovered in production.

**A tool cannot widen its caller's access.** Each declares the scope it needs; the registry
checks the caller has it. Without this, "the planner decided to call patient_lookup" becomes
an authorisation decision made by a classifier.

**Tools return evidence, not prose.** A tool result is a typed record with provenance, so a
computed lab trend can be cited exactly like a retrieved passage — and so nothing can enter
an answer without something an auditor can follow back.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from cip_copilot.domain import Evidence
from cip_core.logging import get_logger

__all__ = [
    "PhiClass",
    "SideEffect",
    "Tool",
    "ToolContext",
    "ToolError",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
    "validate_arguments",
]

_log = get_logger(__name__)


class PhiClass(StrEnum):
    """How much protected health information a tool touches.

    Drives the scope check and what the audit log records. ``PATIENT`` means the tool reads
    an identified patient's record; ``DEIDENTIFIED`` means aggregate or population data;
    ``REFERENCE`` means shared clinical knowledge with no patient in it at all.
    """

    REFERENCE = "reference"
    DEIDENTIFIED = "deidentified"
    PATIENT = "patient"

    @property
    def required_scope(self) -> str:
        return {
            "reference": "reference:read",
            "deidentified": "analytics:read",
            "patient": "patients:read",
        }[self.value]


class SideEffect(StrEnum):
    """Whether running a tool changes anything.

    Every Phase 3 tool is ``READ_ONLY``. The enum exists because the first write-capable tool
    must be a deliberate decision with an approval path attached, not a quiet addition.
    """

    READ_ONLY = "read_only"
    WRITE = "write"


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """A tool's declared contract."""

    name: str
    description: str
    parameters: dict[str, Any]
    """JSON Schema for the arguments object. Also what would be sent to a provider's
    function-calling API, so one declaration serves both."""

    phi_class: PhiClass = PhiClass.REFERENCE
    side_effect: SideEffect = SideEffect.READ_ONLY
    requires_approval: bool = False
    """Suspends the run for a human decision before this tool executes."""
    cost_hint_ms: float = 5.0
    """Rough expected latency, used by the planner to order and bound a plan."""

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_]*", self.name):
            raise ValueError(f"Tool name '{self.name}' must be lower_snake_case")
        if self.parameters.get("type") != "object":
            raise ValueError(f"Tool '{self.name}' parameters must be a JSON Schema object")
        if self.side_effect is SideEffect.WRITE and not self.requires_approval:
            # A write that nobody approved is exactly the class of action that must never be
            # reachable by a planning decision alone.
            raise ValueError(f"Write tool '{self.name}' must require approval")


@dataclass(frozen=True, slots=True)
class ToolContext:
    """Scope and identity for one tool invocation."""

    tenant_id: uuid.UUID
    scopes: frozenset[str]
    patient_id: uuid.UUID | None = None
    request_id: str | None = None

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes


@dataclass(frozen=True, slots=True)
class ToolResult:
    """What a tool produced."""

    tool_name: str
    evidence: tuple[Evidence, ...] = ()
    data: dict[str, Any] = field(default_factory=dict)
    """Structured payload for stages that need the values rather than the citation — the
    timeline builder needs dates, not sentences."""
    note: str = ""

    @property
    def is_empty(self) -> bool:
        return not self.evidence and not self.data


class ToolError(RuntimeError):
    """A tool could not run.

    Carries ``tool_name`` so the aggregator can record *which* capability degraded; a bare
    exception would leave the trace saying only that something failed.
    """

    def __init__(self, message: str, *, tool_name: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.tool_name = tool_name
        self.retryable = retryable


@runtime_checkable
class Tool(Protocol):
    """A callable clinical capability."""

    @property
    def spec(self) -> ToolSpec: ...

    async def run(self, arguments: dict[str, Any], *, context: ToolContext) -> ToolResult: ...


def validate_arguments(spec: ToolSpec, arguments: dict[str, Any]) -> dict[str, Any]:
    """Check ``arguments`` against the tool's schema, returning the coerced object.

    A deliberately small JSON Schema subset — ``type``, ``required``, ``properties``,
    ``enum``, ``minimum``/``maximum`` — rather than a dependency on a full validator. That
    covers every construct these tools actually declare, and it keeps argument validation
    readable at the point where a reviewer is deciding whether a tool is safe to expose.

    Unknown properties are rejected rather than ignored: a planner emitting ``patient`` where
    the schema says ``patient_id`` has made an error, and silently dropping it would run the
    tool against the wrong thing.
    """
    schema = spec.parameters
    properties: dict[str, Any] = schema.get("properties", {})
    required: list[str] = schema.get("required", [])

    missing = [name for name in required if name not in arguments]
    if missing:
        raise ToolError(
            f"Missing required argument(s): {', '.join(sorted(missing))}", tool_name=spec.name
        )

    unknown = [name for name in arguments if name not in properties]
    if unknown:
        raise ToolError(f"Unknown argument(s): {', '.join(sorted(unknown))}", tool_name=spec.name)

    coerced: dict[str, Any] = {}
    for name, value in arguments.items():
        coerced[name] = _coerce(spec.name, name, value, properties[name])
    return coerced


def _coerce(tool_name: str, field_name: str, value: Any, schema: dict[str, Any]) -> Any:
    """Validate and normalise one argument."""
    expected = schema.get("type")

    if expected == "string":
        if not isinstance(value, str):
            raise ToolError(f"'{field_name}' must be a string", tool_name=tool_name)
    elif expected == "integer":
        # bool is a subclass of int in Python; accepting True as an integer would silently
        # turn a malformed argument into the value 1.
        if isinstance(value, bool) or not isinstance(value, int):
            raise ToolError(f"'{field_name}' must be an integer", tool_name=tool_name)
    elif expected == "number":
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ToolError(f"'{field_name}' must be a number", tool_name=tool_name)
    elif expected == "boolean":
        if not isinstance(value, bool):
            raise ToolError(f"'{field_name}' must be a boolean", tool_name=tool_name)
    elif expected == "array":
        if not isinstance(value, list | tuple):
            raise ToolError(f"'{field_name}' must be an array", tool_name=tool_name)
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            return [_coerce(tool_name, f"{field_name}[]", item, item_schema) for item in value]
        return list(value)

    choices = schema.get("enum")
    if choices is not None and value not in choices:
        raise ToolError(
            f"'{field_name}' must be one of {sorted(map(str, choices))}", tool_name=tool_name
        )

    minimum, maximum = schema.get("minimum"), schema.get("maximum")
    if minimum is not None and isinstance(value, int | float) and value < minimum:
        raise ToolError(f"'{field_name}' must be >= {minimum}", tool_name=tool_name)
    if maximum is not None and isinstance(value, int | float) and value > maximum:
        raise ToolError(f"'{field_name}' must be <= {maximum}", tool_name=tool_name)

    return value


class ToolRegistry:
    """Holds the available tools and mediates every call."""

    def __init__(self, tools: list[Tool] | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools or []:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        """Add a tool. Re-registering a name is refused.

        Silent replacement would let a test double or a partially-implemented variant shadow
        the real tool depending on import order, which is the kind of bug that only appears
        under a different collection order.
        """
        name = tool.spec.name
        if name in self._tools:
            raise ValueError(f"Tool '{name}' is already registered")
        self._tools[name] = tool

    def get(self, name: str) -> Tool:
        tool = self._tools.get(name)
        if tool is None:
            raise ToolError(f"Unknown tool '{name}'", tool_name=name)
        return tool

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._tools))

    def specs(self) -> tuple[ToolSpec, ...]:
        return tuple(self._tools[name].spec for name in self.names())

    def as_function_schemas(self) -> list[dict[str, Any]]:
        """Render every spec in provider function-calling form.

        One declaration drives both our validation and a provider's, so the two cannot drift.
        """
        return [
            {
                "type": "function",
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.parameters,
            }
            for spec in self.specs()
        ]

    async def invoke(
        self, name: str, arguments: dict[str, Any], *, context: ToolContext
    ) -> ToolResult:
        """Authorise, validate, and run one tool."""
        tool = self.get(name)
        spec = tool.spec

        required = spec.phi_class.required_scope
        if not context.has_scope(required):
            raise ToolError(
                f"Tool '{name}' requires scope '{required}'", tool_name=name, retryable=False
            )

        validated = validate_arguments(spec, arguments)
        result = await tool.run(validated, context=context)

        foreign = [e.id for e in result.evidence if e.tenant_id != context.tenant_id]
        if foreign:
            # A tool that returns another tenant's data is a defect in that tool, but it
            # must not become an answer. Checked here so every tool is covered by one rule.
            raise ToolError(
                f"Tool '{name}' returned evidence for a different tenant: {foreign}",
                tool_name=name,
            )

        _log.debug(
            "tool.invoked",
            tool=name,
            evidence=len(result.evidence),
            phi_class=str(spec.phi_class),
        )
        return result
