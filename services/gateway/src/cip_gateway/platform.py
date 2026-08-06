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
    "breakers",
    "outbox",
    "postgres",
    "mongo",
    "neo4j",
    "cache",
    "queue",
    "events",
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


# ---------------------------------------------------------------------------------------
# Backing stores.
#
# Constructed here and connected by the container's async lifecycle. Until Phase 9 these were
# built in `cip_ingestion.api.dependencies` — an entirely separate FastAPI application — so the
# unified app had the container, the routes, and the startup validation, and the *other* app had
# the only working database wiring. Neither was deployable.
#
# Construction is synchronous and cannot fail on a network: `PostgresManager(settings)` validates
# a DSN, it does not open a socket. That is what lets the whole platform be built in a unit test
# with no infrastructure, and why `connect` is a separate hook.
# ---------------------------------------------------------------------------------------


def _postgres(container: ServiceContainer) -> Any:
    from cip_core.db.postgres import PostgresManager

    return PostgresManager(container.get("settings")["core"].postgres)


def _mongo(container: ServiceContainer) -> Any:
    from cip_core.db.mongo import MongoManager

    return MongoManager(container.get("settings")["core"].mongo)


def _neo4j(container: ServiceContainer) -> Any:
    from cip_core.db.neo4j import Neo4jManager

    return Neo4jManager(container.get("settings")["core"].neo4j)


def _cache(container: ServiceContainer) -> Any:
    from cip_platform.cache.factory import build_cache

    return build_cache(container.get("settings")["platform"].cache)


def _queue(container: ServiceContainer) -> Any:
    from cip_platform.tasks.factory import build_task_queue

    return build_task_queue(container.get("settings")["platform"].queue)


def _events(container: ServiceContainer) -> Any:
    from cip_platform.events.factory import build_event_bus

    return build_event_bus(container.get("settings")["platform"])


async def _connect_events(bus: Any) -> None:
    """Only the Kafka bus opens a connection; the in-memory one has nothing to open."""
    connect = getattr(bus, "connect", None)
    if connect is not None:
        await connect()


async def _aclose_events(bus: Any) -> None:
    aclose = getattr(bus, "aclose", None)
    if aclose is not None:
        await aclose()


def _breakers(container: ServiceContainer) -> Any:
    """One circuit breaker per external dependency.

    Per-dependency isolation is the whole point: a shared breaker would let a slow knowledge
    graph open the circuit in front of the operational database, turning a degraded feature into
    an outage — the exact failure a breaker exists to prevent.

    Thresholds differ per dependency because their failure modes do. Kafka gets a longer call
    timeout because ``acks=all`` waits for replication; Neo4j opens sooner because the graph is
    non-critical and shedding load from it costs nothing the platform needs.
    """
    from cip_platform.resilience import BreakerConfig, BreakerRegistry

    del container
    return BreakerRegistry(
        configs={
            "postgres": BreakerConfig(call_timeout_seconds=10.0, reset_timeout_seconds=15.0),
            "mongo": BreakerConfig(call_timeout_seconds=10.0, reset_timeout_seconds=15.0),
            # Non-critical, so open earlier and stay open longer: shedding graph load protects
            # a recovering server and costs only enrichment.
            "neo4j": BreakerConfig(
                failure_threshold=0.3, call_timeout_seconds=5.0, reset_timeout_seconds=60.0
            ),
            # A cache is fail-open by design; the breaker exists to stop a slow Redis adding
            # latency to every request, so its timeout is short.
            "redis": BreakerConfig(call_timeout_seconds=2.0, reset_timeout_seconds=10.0),
            # acks=all waits for replication across brokers, so a 10s call is not pathological.
            "kafka": BreakerConfig(call_timeout_seconds=15.0, reset_timeout_seconds=30.0),
        }
    )


def _outbox(container: ServiceContainer) -> Any:
    """The outbox store, and the relay that drains it.

    In-memory when Postgres is not the operational store — which is every unit test and the
    default development path. The relay's code is identical either way, so the tests exercise
    the production path rather than a parallel one.
    """
    from cip_platform.outbox import InMemoryOutboxStore, OutboxPublisher, PublisherConfig
    from cip_platform.resilience import guarded

    settings = container.get("settings")["platform"]
    store: Any = InMemoryOutboxStore()

    return {
        "store": store,
        "publisher": OutboxPublisher(
            store,
            container.get("events"),
            config=PublisherConfig(),
            guard=guarded(container.get("breakers"), "kafka"),
        ),
        "backend": settings.events_backend,
    }


async def _start_relay(outbox: Any) -> None:
    await outbox["publisher"].start()


async def _stop_relay(outbox: Any) -> None:
    await outbox["publisher"].stop()


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
        # Backing stores. Criticality is a product decision, stated per store:
        #
        # postgres  critical — the operational store. Nothing serves without it.
        # mongo     critical — parsed document artifacts; retrieval has nothing to return.
        # neo4j     NOT critical — the graph enriches retrieval and does not gate it. A platform
        #           that refuses to answer questions because a traversal is unavailable has
        #           converted a degraded feature into an outage.
        # cache     NOT critical — a cache outage is latency, and failing closed on it turns a
        #           slowdown into downtime. RedisCache already fails open per request.
        # queue     critical — an unavailable queue that accepts work silently loses it, and the
        #           caller has already been told the document was accepted.
        # events    critical — clinical events must not vanish; see ADR-0026 on ordering.
        .add(
            "postgres",
            _postgres,
            depends_on=("settings",),
            description="Operational store (PostgreSQL)",
            connect=lambda manager: manager.connect(),
            aclose=lambda manager: manager.disconnect(),
        )
        .add(
            "mongo",
            _mongo,
            depends_on=("settings",),
            description="Parsed-document artifact store (MongoDB)",
            connect=lambda manager: manager.connect(),
            aclose=lambda manager: manager.disconnect(),
        )
        .add(
            "neo4j",
            _neo4j,
            depends_on=("settings",),
            critical=False,
            description="Clinical knowledge graph store (Neo4j)",
            connect=lambda manager: manager.connect(),
            aclose=lambda manager: manager.disconnect(),
        )
        .add(
            "cache",
            _cache,
            depends_on=("settings",),
            critical=False,
            description="Shared cache (memory or Redis)",
        )
        .add(
            "queue",
            _queue,
            depends_on=("settings",),
            description="Durable background work queue (memory or Redis)",
        )
        .add(
            "events",
            _events,
            depends_on=("settings",),
            description="Event backbone (memory or Kafka)",
            connect=_connect_events,
            aclose=_aclose_events,
        )
        .add(
            "breakers",
            _breakers,
            depends_on=("settings",),
            description="Circuit breakers, one per external dependency",
        )
        .add(
            "outbox",
            _outbox,
            depends_on=("settings", "events", "breakers"),
            description="Transactional outbox and its publishing relay",
            connect=_start_relay,
            aclose=_stop_relay,
        )
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
