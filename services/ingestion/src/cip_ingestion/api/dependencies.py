"""Application composition and FastAPI dependencies.

:class:`ServiceContainer` builds every collaborator once at startup and holds them for the
process lifetime. Constructing them per request would reopen connection pools and re-probe
the OCR binary on every upload; constructing them at import time would make the module
un-importable without a database, which breaks tests and tooling.

The container is also the test seam: a test builds one with a fake storage backend and a
stub OCR engine and gets the real application with no other changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, Request

from cip_core.config import Settings, get_settings
from cip_core.db.mongo import MongoManager
from cip_core.db.neo4j import Neo4jManager
from cip_core.db.postgres import PostgresManager
from cip_core.errors import AuthenticationError
from cip_core.logging import bind_log_context, get_logger
from cip_core.storage import ObjectStorage, create_storage
from cip_core.tenancy import TenantContext, set_current_tenant_context
from cip_ingestion.api.security import TokenVerifier, build_verifier, context_from_claims
from cip_ingestion.parsers import OcrEngine, build_parser_registry
from cip_ingestion.pipeline import IngestionPipeline
from cip_ingestion.processor import DocumentProcessor

__all__ = [
    "CurrentContext",
    "ServiceContainer",
    "get_container",
    "get_tenant_context",
]

_log = get_logger(__name__)


@dataclass(slots=True)
class ServiceContainer:
    """Process-lifetime collaborators."""

    settings: Settings
    postgres: PostgresManager
    mongo: MongoManager
    neo4j: Neo4jManager
    storage: ObjectStorage
    pipeline: IngestionPipeline
    verifier: TokenVerifier

    @classmethod
    def build(
        cls,
        settings: Settings | None = None,
        *,
        storage: ObjectStorage | None = None,
        ocr_engine: OcrEngine | None = None,
    ) -> ServiceContainer:
        """Compose the service graph."""
        settings = settings or get_settings()

        postgres = PostgresManager(settings.postgres)
        mongo = MongoManager(settings.mongo)
        neo4j = Neo4jManager(settings.neo4j)
        object_storage = storage or create_storage(settings.storage)

        processor = DocumentProcessor(
            parsers=build_parser_registry(settings.ingestion, ocr_engine=ocr_engine),
            settings=settings.ingestion,
        )
        pipeline = IngestionPipeline(
            settings=settings,
            postgres=postgres,
            mongo=mongo,
            storage=object_storage,
            processor=processor,
        )
        return cls(
            settings=settings,
            postgres=postgres,
            mongo=mongo,
            neo4j=neo4j,
            storage=object_storage,
            pipeline=pipeline,
            verifier=build_verifier(settings.auth),
        )

    async def startup(self) -> None:
        """Open connections.

        Connections are opened but not verified here. A backing service that is briefly
        unavailable at pod start should not crash-loop the pod — readiness reports the
        dependency as down and Kubernetes withholds traffic until it recovers, which is
        the behaviour the health endpoints exist to provide.
        """
        await self.postgres.connect()
        await self.mongo.connect()
        await self.neo4j.connect()
        _log.info("service.started", **self.settings.describe())

    async def shutdown(self) -> None:
        """Close connections, tolerating failures so shutdown always completes."""
        for name, close in (
            ("neo4j", self.neo4j.disconnect),
            ("mongo", self.mongo.disconnect),
            ("postgres", self.postgres.disconnect),
        ):
            try:
                await close()
            except Exception:
                _log.exception("service.shutdown_error", dependency=name)
        _log.info("service.stopped")


def get_container(request: Request) -> ServiceContainer:
    """Return the container attached to the running application."""
    container = getattr(request.app.state, "container", None)
    if container is None:  # pragma: no cover - indicates a wiring bug, not a runtime path
        raise RuntimeError("ServiceContainer is not attached to the application")
    return container  # type: ignore[no-any-return]


async def get_tenant_context(
    request: Request,
    container: Annotated[ServiceContainer, Depends(get_container)],
    authorization: Annotated[str | None, Header()] = None,
) -> TenantContext:
    """Authenticate the caller and derive its tenant context.

    When ``CIP_AUTH__ENABLED`` is false — local development only, rejected by ``Settings``
    in deployed environments — an unauthenticated caller is refused rather than granted a
    default tenant. Disabling auth removes the *requirement to present a token*, not the
    requirement to identify a tenant; inventing one would make cross-tenant tests pass
    that should fail.
    """
    request_id = getattr(request.state, "request_id", None)

    if not container.settings.auth.enabled:
        raise AuthenticationError(
            "Authentication is disabled; supply an explicit tenant context via the CLI "
            "instead of the HTTP API"
        )

    if not authorization or not authorization.lower().startswith("bearer "):
        raise AuthenticationError("Missing or malformed Authorization header")

    token = authorization.split(" ", 1)[1].strip()
    claims = container.verifier.verify(token)
    context = context_from_claims(claims, request_id=request_id)

    set_current_tenant_context(context)
    bind_log_context(tenant_id=str(context.tenant_id), actor_id=context.actor_id)
    return context


CurrentContext = Annotated[TenantContext, Depends(get_tenant_context)]
