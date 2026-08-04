"""Builds engines from a loaded knowledge base.

The seam between *content* and *machinery*. The loader validates and parses; this turns the
parsed artifacts into the typed objects each engine consumes. Keeping them separate means a
new engine needs a factory function rather than changes to the loader, and the loader stays
the single place that decides whether an artifact is acceptable.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from cip_decision.domain import Citation
from cip_decision.knowledge.loader import KnowledgeBase, parse_condition
from cip_decision.pathways.engine import CarePathway, PathwayAction, PathwayStage
from cip_decision.risk.scoring import RiskBand, RiskComponent, RiskModel
from cip_decision.rules.engine import RuleEngine

__all__ = ["build_pathways", "build_risk_models", "build_rule_engine", "citations_from"]


def citations_from(raw: Any) -> tuple[Citation, ...]:
    """Parse a citation list from a knowledge artifact."""
    citations: list[Citation] = []
    for item in raw or []:
        if isinstance(item, str):
            citations.append(Citation(source=item))
            continue
        published = item.get("published")
        citations.append(
            Citation(
                source=item["source"],
                reference=item.get("reference", ""),
                url=item.get("url", ""),
                published=dt.date.fromisoformat(str(published)) if published else None,
                note=item.get("note", ""),
            )
        )
    return tuple(citations)


def build_rule_engine(base: KnowledgeBase) -> RuleEngine:
    """A rule engine holding every loaded rule, active or not.

    Inactive rules are registered rather than filtered here, because the engine reports them
    as skipped — and "the rule exists but is not in effect today" is a different answer from
    "there is no such rule" when somebody asks why they were not warned.
    """
    return RuleEngine(list(base.rules))


def build_risk_models(base: KnowledgeBase) -> tuple[RiskModel, ...]:
    """Typed risk models from their declarative form."""
    models: list[RiskModel] = []
    for raw in base.risk_models:
        components = tuple(
            RiskComponent(
                component_id=c["id"],
                label=c["label"],
                points=int(c["points"]),
                condition=parse_condition(c["when"], where=f"{raw['id']}.{c['id']}"),
            )
            for c in raw["components"]
        )
        bands = tuple(
            RiskBand(at_least=int(b["at_least"]), band=b["band"], note=b.get("note", ""))
            for b in raw.get("interpretation", [])
        )
        models.append(
            RiskModel(
                model_id=raw["id"],
                version=str(raw["version"]),
                title=raw["title"],
                components=components,
                citations=citations_from(raw["citations"]),
                bands=bands,
                guideline_id=raw.get("guideline", ""),
                applies_when=(
                    parse_condition(raw["applies_when"], where=f"{raw['id']}.applies_when")
                    if raw.get("applies_when")
                    else None
                ),
            )
        )
    return tuple(models)


def build_pathways(base: KnowledgeBase) -> tuple[CarePathway, ...]:
    """Typed care pathways from their declarative form."""
    pathways: list[CarePathway] = []
    for raw in base.pathways:
        pathways.append(
            CarePathway(
                pathway_id=raw["id"],
                version=str(raw["version"]),
                title=raw["title"],
                actions=tuple(_build_action(a, where=raw["id"]) for a in raw["actions"]),
                citations=citations_from(raw["citations"]),
                trigger=(
                    parse_condition(raw["trigger"], where=f"{raw['id']}.trigger")
                    if raw.get("trigger")
                    else None
                ),
                guideline_id=raw.get("guideline", ""),
                effective_from=(
                    dt.date.fromisoformat(str(raw["effective_from"]))
                    if raw.get("effective_from")
                    else None
                ),
                effective_until=(
                    dt.date.fromisoformat(str(raw["effective_until"]))
                    if raw.get("effective_until")
                    else None
                ),
            )
        )
    return tuple(pathways)


def _build_action(raw: dict[str, Any], *, where: str) -> PathwayAction:
    return PathwayAction(
        action_id=raw["id"],
        title=raw["title"],
        stage=PathwayStage(raw["stage"]),
        description=raw.get("description", ""),
        condition=(
            parse_condition(raw["when"], where=f"{where}.{raw['id']}.when")
            if raw.get("when")
            else None
        ),
        children=tuple(
            _build_action(child, where=f"{where}.{raw['id']}") for child in raw.get("actions", [])
        ),
    )
