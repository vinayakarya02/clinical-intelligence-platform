"""Platform assembly: the registration list for every service.

This module and :mod:`cip_gateway.container` are the only places permitted to import more than
one service. A boundary test enforces it, because the value of six services that do not know
about each other is lost the moment a seventh place starts wiring them together.

Every import is **inside a factory**, not at module top level. Three reasons, and the third is
the one that matters operationally:

- a process that needs only the analytics surface does not pay for the interop imports
- a broken module takes down the service that needs it, not the whole platform
- the failure surfaces as *that service failed to start*, with its name, rather than as an
  ImportError during module load with a traceback into somebody else's package

Criticality is declared per service. The retrieval and copilot stack is critical — a clinical
platform that cannot answer questions is down. Analytics is not: an empty warehouse is a
reporting gap, and refusing to serve clinicians over it would be the wrong trade.
"""

from __future__ import annotations

import datetime as dt
import os
import pathlib
from typing import TYPE_CHECKING, Any

from cip_core.logging import get_logger
from cip_gateway.container import ContainerBuilder, ServiceContainer

if TYPE_CHECKING:  # pragma: no cover - import-time typing only
    pass

__all__ = [
    "SERVICE_NAMES",
    "build_platform",
    "platform_specs",
]

_log = get_logger(__name__)

#: Service names, in one place so health, routes, and tests agree on the vocabulary. A typo'd
#: name in a health check reports on a service that does not exist and reports nothing about the
#: one that does.
SERVICE_NAMES = (
    "settings",
    "gateway",
    "audit",
    "ingestion",
    "retrieval",
    "knowledge_graph",
    "copilot",
    "decision",
    "interop",
    "analytics",
)

_SERVICES = pathlib.Path(__file__).resolve().parents[3]
_DECISION_CORPUS = _SERVICES / "decision/src/cip_decision/knowledge/corpus"
_ANALYTICS_CATALOGUE = _SERVICES / "analytics/src/cip_analytics/metrics/catalogue.yaml"


def _settings(_: ServiceContainer) -> Any:
    """Both settings objects, validated at startup.

    ``cip_core`` settings configure the domain services; ``cip_platform`` settings configure
    cache, queue, telemetry, and limits. Loading both here means a misconfiguration fails the
    *first* service to start rather than the first request to arrive.

    Mounted secrets are read **first**, and this ordering is the whole point of doing it in the
    composition root: a Kubernetes Secret arrives as a directory of files, both settings systems
    read the environment, and until this call existed the two never met. Every secret-derived
    setting held its default in production.
    """
    from cip_core.config import get_settings
    from cip_core.secrets import load_mounted_secrets
    from cip_platform.config import load_platform_settings

    secrets = load_mounted_secrets()
    if secrets.present:
        _log.info("platform.secrets", detail=secrets.render())

    return {
        "core": get_settings(),
        "platform": load_platform_settings(),
        "secrets": secrets,
    }


def _audit(_: ServiceContainer) -> Any:
    from cip_interop.consent import InMemoryAuditSink

    return InMemoryAuditSink()


def _ingestion(container: ServiceContainer) -> Any:
    """The document-intelligence processor.

    Phase 1's pure stage orchestration, which does the parse/normalise/section/chunk work with
    no I/O. The I/O-bearing pipeline needs a database and is wired separately by a deployment
    that has one.
    """
    from cip_ingestion.parsers import build_parser_registry
    from cip_ingestion.processor import DocumentProcessor

    settings = container.get("settings")["core"]
    return DocumentProcessor(
        parsers=build_parser_registry(settings.ingestion),
        settings=settings.ingestion,
    )


def _retrieval(container: ServiceContainer) -> Any:
    from cip_retrieval.embeddings import EmbeddingService, HashingEmbeddingProvider
    from cip_retrieval.vectorstore import InMemoryVectorStore

    del container
    provider = HashingEmbeddingProvider()
    return {
        "provider": provider,
        "embeddings": EmbeddingService(provider=provider),
        "vector_store": InMemoryVectorStore(),
    }


def _knowledge_graph(_: ServiceContainer) -> Any:
    from cip_retrieval.graph import InMemoryGraphStore

    return InMemoryGraphStore()


def _copilot(container: ServiceContainer) -> Any:
    from cip_copilot.llm import ExtractiveLanguageModel

    del container
    return {"language_model": ExtractiveLanguageModel()}


def _decision(_: ServiceContainer) -> Any:
    from cip_decision.drugs.intelligence import DrugIntelligence
    from cip_decision.engine import DecisionEngine
    from cip_decision.factory import build_pathways, build_risk_models, build_rule_engine
    from cip_decision.knowledge.loader import load_knowledge_base
    from cip_decision.pathways.engine import PathwayEngine
    from cip_decision.risk.scoring import RiskScorer

    base = load_knowledge_base(_DECISION_CORPUS)
    return DecisionEngine(
        rules=build_rule_engine(base),
        drugs=DrugIntelligence(interactions=base.interactions),
        risk=RiskScorer(build_risk_models(base)),
        pathways=PathwayEngine(build_pathways(base)),
    )


def _interop(container: ServiceContainer) -> Any:
    from cip_interop.api import ClinicalApi
    from cip_interop.consent import ConsentEngine
    from cip_interop.empi.index import EmpiIndex
    from cip_interop.fhir.repository import RepositoryRegistry
    from cip_interop.orgs import AgreementRegistry, OrganizationDirectory
    from cip_interop.routing import IntegrationEngine
    from cip_interop.streaming import EventStream

    empi = EmpiIndex()
    repositories = RepositoryRegistry()
    stream = EventStream(partitions=8)
    consent = ConsentEngine(audit_sink=container.get("audit"))
    directory = OrganizationDirectory()
    agreements = AgreementRegistry(directory)

    # Required by ``ClinicalApi`` rather than defaulted, and this is the wiring that makes it
    # real: consent is filed against a person, FHIR ids are organisation-local, and looking
    # consent up under the local id lets a revocation at one organisation leave another still
    # disclosing (docs/design/adr-0030-cross-organisation-sharing.md).
    resolve_person = empi.person_for_resource

    return {
        "empi": empi,
        "directory": directory,
        "repositories": repositories,
        "stream": stream,
        "consent": consent,
        "agreements": agreements,
        "engine": IntegrationEngine(empi=empi, repositories=repositories, stream=stream),
        # The HTTP surface. Phases 6 and 7 built these and never constructed them here, which
        # is why both APIs were unreachable: the container had no object to route to.
        "api": ClinicalApi(
            repositories=repositories,
            consent=consent,
            agreements=agreements,
            resolve_person=resolve_person,
        ),
    }


def _deidentification_salt(container: ServiceContainer) -> str:
    """The salt the warehouse pseudonymises with.

    This is the one configuration value whose default is not merely weak but actively harmful.
    Pseudonyms are ``HMAC(salt, identifier)``: anyone who knows the salt and holds a list of
    candidate MRNs can recompute every key in the warehouse and re-identify a dataset that was
    built specifically to be de-identifiable. A salt committed to the repository is a salt
    everyone knows.

    Development gets a deterministic default so the demo and the tests reproduce. Production
    gets nothing — :func:`cip_gateway.startup.validate_startup` fails the process before it
    serves, rather than letting it come up and quietly write reversible keys.
    """
    import os

    salt = os.environ.get("CIP_ANALYTICS_SALT", "").strip()
    if salt:
        return salt

    environment = str(container.get("settings")["platform"].environment)
    if environment == "production":
        raise RuntimeError(
            "CIP_ANALYTICS_SALT is unset in production; refusing to build the warehouse with a "
            "known salt, which would make every pseudonym reversible"
        )
    return "development-only-salt"


def _analytics(container: ServiceContainer) -> Any:
    from cip_analytics.api import AnalyticsApi
    from cip_analytics.boards import DashboardRegistry
    from cip_analytics.etl import Pipeline
    from cip_analytics.query import QueryExecutor, TemplateRegistry
    from cip_analytics.semantic import load_metrics
    from cip_analytics.warehouse import Warehouse, default_schema

    schema = default_schema()
    warehouse = Warehouse(schema)
    metrics = load_metrics(_ANALYTICS_CATALOGUE, schema)
    templates = TemplateRegistry(metrics)
    executor = QueryExecutor(warehouse, metrics, templates)
    dashboards = DashboardRegistry(metrics)
    return {
        "warehouse": warehouse,
        "metrics": metrics,
        "templates": templates,
        "dashboards": dashboards,
        "executor": executor,
        "etl": Pipeline(warehouse, salt=_deidentification_salt(container)),
        "api": AnalyticsApi(
            executor=executor, metrics=metrics, templates=templates, dashboards=dashboards
        ),
    }


def _gateway(container: ServiceContainer) -> Any:
    """Phase 3's guards, wired to the configured limits.

    Registered as a service rather than constructed inside the HTTP layer for the reason the
    whole container exists: the limits come from configuration, and a guard built at import time
    is a guard built before the configuration was read. It also means the rate limiter is a
    single instance shared by every route, which is the only arrangement in which a per-tenant
    limit actually limits anything.
    """
    from cip_gateway.middleware import GatewayGuards
    from cip_platform.security.identity import ApiKeyStore
    from cip_platform.security.limits import SpendBudget, TokenBucketLimiter

    settings = container.get("settings")
    limits = settings["platform"].limits
    pepper = os.environ.get("CIP_AUTH__API_KEY_PEPPER", "") or "development-only-pepper"

    keys = ApiKeyStore(pepper=pepper)
    return {
        "api_keys": keys,
        "guards": GatewayGuards(
            api_keys=keys,
            tenant_limiter=TokenBucketLimiter(
                requests_per_minute=limits.requests_per_minute_per_tenant,
                burst_multiplier=limits.burst_multiplier,
            ),
            principal_limiter=TokenBucketLimiter(
                requests_per_minute=limits.requests_per_minute_per_principal,
                burst_multiplier=limits.burst_multiplier,
            ),
            budget=SpendBudget(daily_limit_usd=limits.daily_budget_usd_per_tenant),
            max_request_bytes=limits.max_request_bytes,
        ),
    }


def platform_specs() -> ContainerBuilder:
    """Every service the platform runs, with its dependencies and criticality."""
    return (
        ContainerBuilder()
        .add("settings", _settings, description="Validated platform configuration")
        .add("audit", _audit, description="Audit sink shared by disclosure paths")
        .add(
            "ingestion",
            _ingestion,
            depends_on=("settings",),
            description="Document intelligence: parse, normalise, section, chunk",
        )
        .add(
            "retrieval",
            _retrieval,
            depends_on=("settings",),
            description="Embeddings and the vector store",
        )
        .add(
            "knowledge_graph",
            _knowledge_graph,
            depends_on=("settings",),
            description="Clinical knowledge graph",
        )
        .add(
            "copilot",
            _copilot,
            depends_on=("retrieval", "knowledge_graph"),
            description="Clinical copilot language seam",
        )
        .add(
            "decision",
            _decision,
            depends_on=("settings",),
            description="Clinical decision intelligence",
        )
        .add(
            "interop",
            _interop,
            depends_on=("settings", "audit"),
            description="HL7, FHIR, EMPI, streaming",
        )
        .add(
            "analytics",
            _analytics,
            depends_on=("settings",),
            critical=False,
            description="Analytics warehouse and semantic layer",
        )
        .add(
            "gateway",
            _gateway,
            depends_on=("settings",),
            description="Authentication, rate limiting, and spend budget",
        )
    )


def build_platform() -> ServiceContainer:
    """A container with every service registered but nothing built yet."""
    return platform_specs().build()


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)
