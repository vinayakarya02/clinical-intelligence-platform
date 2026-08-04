"""Knowledge loading and validation.

Turns versioned YAML into typed clinical artifacts. This is the only place clinical content
enters the system, and it is deliberately strict: an artifact that cannot be validated is
refused rather than partially loaded, because a knowledge base that silently drops rules is a
knowledge base whose coverage nobody can state.

Three refusals matter:

**No citation, no load.** An uncited clinical assertion cannot be reviewed or defended
(docs/design/adr-0019-knowledge-as-data.md).

**No ``eval``, ever.** Conditions are parsed into the typed AST. A knowledge base is a file an
operator edits, and evaluating it as Python is remote code execution with a clinical veneer.

**Unknown keys are errors.** A misspelled ``severity_`` would otherwise default silently, and
a rule that quietly downgraded itself from ``contraindicated`` is the worst failure this
loader can permit.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from cip_core.errors import CipError
from cip_core.logging import get_logger
from cip_decision.domain import Citation, EvidenceQuality, FactKind, RecommendationKind, Severity
from cip_decision.rules.ast import (
    AgeBetween,
    AllOf,
    Always,
    AnyOf,
    Comparison,
    Condition,
    HasFact,
    Not,
    Operator,
    Trend,
    TrendDirection,
    ValueOf,
    WithinDays,
)
from cip_decision.rules.engine import ClinicalRule

__all__ = [
    "KnowledgeBase",
    "KnowledgeError",
    "load_knowledge_base",
    "parse_condition",
]

_log = get_logger(__name__)

#: Keys a rule artifact may declare. An unknown key is an error, because a typo that silently
#: defaults is how a rule downgrades itself without anyone noticing.
_RULE_KEYS = frozenset(
    {
        "id",
        "version",
        "title",
        "when",
        "recommend",
        "severity",
        "evidence_quality",
        "kind",
        "detail",
        "citations",
        "effective_from",
        "effective_until",
        "guideline",
        "tags",
        "concern",
        "supersedes",
        "direction",
    }
)

_CONDITION_KEYS = frozenset(
    {
        "all",
        "any",
        "not",
        "has",
        "absent",
        "compare",
        "trend",
        "within_days",
        "age",
        "always",
    }
)


class KnowledgeError(CipError):
    """A knowledge artifact is invalid."""

    status = 500
    problem_type = "knowledge-invalid"
    title = "Clinical knowledge artifact is invalid"


def _require(payload: dict[str, Any], key: str, *, where: str) -> Any:
    if key not in payload:
        raise KnowledgeError(f"{where}: missing required key '{key}'")
    return payload[key]


def _reject_unknown(payload: dict[str, Any], allowed: frozenset[str], *, where: str) -> None:
    unknown = set(payload) - allowed
    if unknown:
        raise KnowledgeError(
            f"{where}: unknown key(s) {sorted(unknown)}. A misspelled key would silently "
            f"take a default, which for 'severity' means a contraindication becoming an "
            f"informational note."
        )


def _parse_date(value: Any, *, where: str) -> dt.date | None:
    if value is None:
        return None
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value))
    except ValueError as exc:
        raise KnowledgeError(f"{where}: '{value}' is not an ISO date") from exc


def _parse_citations(raw: Any, *, where: str) -> tuple[Citation, ...]:
    if not raw:
        raise KnowledgeError(
            f"{where}: at least one citation is required. An uncited clinical assertion "
            "cannot be reviewed or defended."
        )
    citations: list[Citation] = []
    for entry in raw:
        if isinstance(entry, str):
            citations.append(Citation(source=entry))
            continue
        citations.append(
            Citation(
                source=_require(entry, "source", where=where),
                reference=entry.get("reference", ""),
                url=entry.get("url", ""),
                published=_parse_date(entry.get("published"), where=where),
                note=entry.get("note", ""),
            )
        )
    return tuple(citations)


def parse_condition(payload: Any, *, where: str = "condition") -> Condition:
    """Build a typed condition from its declarative form.

    Exhaustive over the supported forms and strict about everything else. An unrecognised
    condition raises rather than evaluating to false, because a rule that silently never fires
    is indistinguishable from a rule that is working.
    """
    if not isinstance(payload, dict):
        raise KnowledgeError(
            f"{where}: a condition must be a mapping, got {type(payload).__name__}"
        )

    unknown = set(payload) - _CONDITION_KEYS
    if unknown:
        # A precise message here, not the generic unknown-key one: for a condition the
        # actionable fact is *which operator* is unsupported, and the generic message made
        # the specific error below unreachable.
        raise KnowledgeError(
            f"{where}: unsupported condition operator(s) {sorted(unknown)}. "
            f"Supported: {sorted(_CONDITION_KEYS)}"
        )
    if len(payload) != 1:
        raise KnowledgeError(
            f"{where}: a condition must have exactly one operator, got {sorted(payload)}"
        )

    operator, body = next(iter(payload.items()))

    if operator == "all":
        return AllOf(tuple(parse_condition(c, where=f"{where}.all") for c in body))
    if operator == "any":
        return AnyOf(tuple(parse_condition(c, where=f"{where}.any") for c in body))
    if operator == "not":
        return Not(parse_condition(body, where=f"{where}.not"))
    if operator == "always":
        return Always(reason=str(body) if isinstance(body, str) else "unconditional")

    if operator == "has":
        return HasFact(
            kind=FactKind(_require(body, "kind", where=where)),
            name=body.get("name", ""),
            code=body.get("code"),
            active_only=bool(body.get("active_only", True)),
            within_days=body.get("within_days"),
        )
    if operator == "absent":
        return Not(
            HasFact(
                kind=FactKind(_require(body, "kind", where=where)),
                name=body.get("name", ""),
                code=body.get("code"),
                active_only=bool(body.get("active_only", True)),
            )
        )
    if operator == "compare":
        return Comparison(
            subject=ValueOf(
                name=_require(body, "observation", where=where),
                within_days=body.get("within_days"),
            ),
            operator=Operator(_require(body, "op", where=where)),
            threshold=float(_require(body, "value", where=where)),
            unit=body.get("unit", ""),
        )
    if operator == "trend":
        return Trend(
            name=_require(body, "observation", where=where),
            direction=TrendDirection(_require(body, "direction", where=where)),
            minimum_change_ratio=float(body.get("minimum_change_ratio", 0.05)),
            minimum_points=int(body.get("minimum_points", 2)),
        )
    if operator == "within_days":
        return WithinDays(
            kind=FactKind(_require(body, "kind", where=where)),
            name=_require(body, "name", where=where),
            days=int(_require(body, "days", where=where)),
        )
    if operator == "age":
        return AgeBetween(minimum=body.get("min"), maximum=body.get("max"))

    raise KnowledgeError(f"{where}: unsupported condition operator '{operator}'")


def _parse_rule(payload: dict[str, Any], *, where: str) -> ClinicalRule:
    _reject_unknown(payload, _RULE_KEYS, where=where)
    rule_id = _require(payload, "id", where=where)

    return ClinicalRule(
        rule_id=rule_id,
        version=str(_require(payload, "version", where=where)),
        title=_require(payload, "title", where=where),
        condition=parse_condition(_require(payload, "when", where=where), where=f"{where}.when"),
        recommendation_summary=_require(payload, "recommend", where=where),
        severity=Severity(_require(payload, "severity", where=where)),
        evidence_quality=EvidenceQuality(_require(payload, "evidence_quality", where=where)),
        citations=_parse_citations(payload.get("citations"), where=where),
        kind=RecommendationKind(payload.get("kind", "alert")),
        detail=payload.get("detail", ""),
        effective_from=_parse_date(payload.get("effective_from"), where=where),
        effective_until=_parse_date(payload.get("effective_until"), where=where),
        guideline_id=payload.get("guideline", ""),
        tags=tuple(payload.get("tags", ())),
        concern=payload.get("concern", ""),
        direction=payload.get("direction", ""),
        supersedes=tuple(payload.get("supersedes", ())),
    )


@dataclass(frozen=True, slots=True)
class KnowledgeBase:
    """Everything loaded from a knowledge directory.

    Carries its own provenance — which files it came from and when — so a deployed system can
    report which knowledge it is running, which is the first question after an unexpected
    recommendation.
    """

    rules: tuple[ClinicalRule, ...] = ()
    interactions: tuple[dict[str, Any], ...] = ()
    guidelines: tuple[dict[str, Any], ...] = ()
    risk_models: tuple[dict[str, Any], ...] = ()
    pathways: tuple[dict[str, Any], ...] = ()
    sources: tuple[str, ...] = ()
    loaded_at: dt.datetime | None = None

    def active_rules(self, on: dt.date) -> tuple[ClinicalRule, ...]:
        return tuple(r for r in self.rules if r.is_active(on))

    def describe(self) -> str:
        return (
            f"{len(self.rules)} rules, {len(self.interactions)} interactions, "
            f"{len(self.guidelines)} guidelines, {len(self.risk_models)} risk models, "
            f"{len(self.pathways)} pathways from {len(self.sources)} file(s)"
        )


def load_knowledge_base(directory: Path) -> KnowledgeBase:
    """Load and validate every knowledge file in ``directory``.

    **The corpus shipped with this repository is a demonstration set that has not been
    clinically reviewed** (docs/safety/clinical-safety-case.md). This loader validates
    structure; nothing here validates clinical correctness, and nothing can.

    Fails on the first invalid artifact rather than skipping it. A knowledge base that
    partially loads has a coverage nobody can state, and "the rule was there but did not
    load" is the worst possible explanation after an incident.
    """
    if not directory.is_dir():
        raise KnowledgeError(f"Knowledge directory not found: {directory}")

    rules: list[ClinicalRule] = []
    interactions: list[dict[str, Any]] = []
    guidelines: list[dict[str, Any]] = []
    risk_models: list[dict[str, Any]] = []
    pathways: list[dict[str, Any]] = []
    sources: list[str] = []

    seen_rule_keys: set[str] = set()

    for path in sorted(directory.glob("*.yaml")):
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        sources.append(path.name)
        where = path.name

        for raw in payload.get("rules", []):
            rule = _parse_rule(raw, where=f"{where}#{raw.get('id', '?')}")
            if rule.key in seen_rule_keys:
                raise KnowledgeError(
                    f"{where}: rule {rule.key} is defined more than once; which one is active "
                    "would depend on file order"
                )
            seen_rule_keys.add(rule.key)
            rules.append(rule)

        for raw in payload.get("interactions", []):
            _validate_interaction(raw, where=where)
            interactions.append(raw)

        for raw in payload.get("guidelines", []):
            _validate_guideline(raw, where=where)
            guidelines.append(raw)

        for raw in payload.get("risk_models", []):
            _validate_risk_model(raw, where=where)
            risk_models.append(raw)

        for raw in payload.get("pathways", []):
            _validate_pathway(raw, where=where)
            pathways.append(raw)

    base = KnowledgeBase(
        rules=tuple(rules),
        interactions=tuple(interactions),
        guidelines=tuple(guidelines),
        risk_models=tuple(risk_models),
        pathways=tuple(pathways),
        sources=tuple(sources),
        loaded_at=dt.datetime.now(dt.UTC),
    )
    _log.info("knowledge.loaded", summary=base.describe())
    return base


def _validate_interaction(raw: dict[str, Any], *, where: str) -> None:
    for key in ("id", "left", "right", "severity", "evidence_quality", "effect", "citations"):
        _require(raw, key, where=f"{where} interaction {raw.get('id', '?')}")
    Severity(raw["severity"])
    EvidenceQuality(raw["evidence_quality"])
    _parse_citations(raw["citations"], where=f"{where} interaction {raw['id']}")


def _validate_guideline(raw: dict[str, Any], *, where: str) -> None:
    for key in ("id", "version", "title", "publisher", "effective_from", "citations"):
        _require(raw, key, where=f"{where} guideline {raw.get('id', '?')}")
    _parse_citations(raw["citations"], where=f"{where} guideline {raw['id']}")
    _parse_date(raw["effective_from"], where=where)
    _parse_date(raw.get("effective_until"), where=where)


def _validate_risk_model(raw: dict[str, Any], *, where: str) -> None:
    label = f"{where} risk model {raw.get('id', '?')}"
    for key in ("id", "version", "title", "components", "citations"):
        _require(raw, key, where=label)
    _parse_citations(raw["citations"], where=label)
    if raw.get("applies_when"):
        parse_condition(raw["applies_when"], where=f"{label}.applies_when")
    if not raw["components"]:
        raise KnowledgeError(f"{label}: a risk model needs at least one component")
    for component in raw["components"]:
        for key in ("id", "label", "points", "when"):
            _require(component, key, where=f"{label} component {component.get('id', '?')}")
        parse_condition(component["when"], where=f"{label}.{component['id']}.when")


def _validate_pathway(raw: dict[str, Any], *, where: str) -> None:
    label = f"{where} pathway {raw.get('id', '?')}"
    for key in ("id", "version", "title", "actions", "citations"):
        _require(raw, key, where=label)
    _parse_citations(raw["citations"], where=label)
    _validate_actions(raw["actions"], where=label)


def _validate_actions(actions: list[dict[str, Any]], *, where: str) -> None:
    for action in actions:
        label = f"{where} action {action.get('id', '?')}"
        for key in ("id", "title", "stage"):
            _require(action, key, where=label)
        if "when" in action:
            parse_condition(action["when"], where=f"{label}.when")
        if action.get("actions"):
            _validate_actions(action["actions"], where=label)
