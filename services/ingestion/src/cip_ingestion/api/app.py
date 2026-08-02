"""FastAPI application factory.

A factory rather than a module-level ``app`` so the application can be constructed with
injected collaborators (a fake storage backend, a stub OCR engine) in tests, and so
importing this module does not require a database.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from cip_core.config import Settings, get_settings
from cip_core.errors import CipError
from cip_core.logging import configure_logging, get_logger
from cip_core.storage import ObjectStorage
from cip_ingestion.api.dependencies import ServiceContainer
from cip_ingestion.api.middleware import (
    RequestContextMiddleware,
    cip_error_handler,
    unhandled_error_handler,
)
from cip_ingestion.api.routes import documents, health
from cip_ingestion.parsers import OcrEngine
from cip_ingestion.version import SERVICE_VERSION

__all__ = ["create_app"]

_log = get_logger(__name__)

_DESCRIPTION = """
Phase 1 Document Intelligence service for the Clinical Intelligence Platform.

Ingests clinical documents (PDF, DOCX, plain text), parses them with layout awareness and
OCR fallback, normalises and sections the text, extracts metadata, chunks the content, and
persists the results with a data-quality verdict.

Embedding generation, vector search, knowledge-graph construction, retrieval, and
conversational AI are later phases and are not served by this API.
""".strip()


def create_app(
    settings: Settings | None = None,
    *,
    container: ServiceContainer | None = None,
    storage: ObjectStorage | None = None,
    ocr_engine: OcrEngine | None = None,
) -> FastAPI:
    """Build the ingestion API.

    ``container`` takes precedence over ``storage``/``ocr_engine``: a caller supplying a
    fully-built container has already made those choices.
    """
    settings = settings or get_settings()
    configure_logging(settings)

    service_container = container or ServiceContainer.build(
        settings, storage=storage, ocr_engine=ocr_engine
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await service_container.startup()
        try:
            yield
        finally:
            await service_container.shutdown()

    app = FastAPI(
        title="Clinical Intelligence Platform — Ingestion API",
        description=_DESCRIPTION,
        version=SERVICE_VERSION,
        lifespan=lifespan,
        # Interactive docs are disabled outside development: they describe the full API
        # surface to anyone who can reach the service, which is unnecessary exposure in a
        # deployment where clients are integrations working from the published spec.
        docs_url="/docs" if not settings.environment.is_deployed else None,
        redoc_url=None,
        openapi_url="/openapi.json" if not settings.environment.is_deployed else None,
    )

    app.state.container = service_container
    app.state.settings = settings

    app.add_middleware(RequestContextMiddleware)

    # CipError first (specific), Exception last (catch-all). Starlette dispatches on the
    # most specific registered class, so ordering here is documentation rather than logic.
    app.add_exception_handler(CipError, cip_error_handler)
    app.add_exception_handler(Exception, unhandled_error_handler)

    app.include_router(health.router)
    app.include_router(documents.router)

    _log.info("app.created", environment=str(settings.environment), version=SERVICE_VERSION)
    return app
