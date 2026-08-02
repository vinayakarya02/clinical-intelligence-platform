"""Health endpoints.

Liveness and readiness answer different questions and must not share an implementation.

*Liveness* asks "is this process working?" It touches no dependency, because a database
outage is not a reason to restart an otherwise-healthy pod — restarting it removes
capacity that would recover the moment the dependency does, and in a shared-database
deployment a liveness probe wired to the database restarts every pod simultaneously.

*Readiness* asks "should this pod receive traffic?" It checks every dependency the
request path needs, so a pod that cannot serve is removed from rotation while staying
alive to recover.

Neither endpoint requires authentication (they expose no tenant data) and both are
excluded from access logging.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Response, status

from cip_core.errors import DependencyUnavailableError
from cip_core.logging import get_logger
from cip_ingestion.api.dependencies import ServiceContainer, get_container
from cip_ingestion.api.schemas import HealthResponse
from cip_ingestion.version import SERVICE_VERSION

__all__ = ["router"]

_log = get_logger(__name__)

router = APIRouter(prefix="/health", tags=["Health"])

#: Readiness must answer faster than the probe timeout, or a slow dependency turns into a
#: probe timeout that looks identical to a crashed pod.
_READINESS_TIMEOUT_SECONDS = 5.0


@router.get("/live", response_model=HealthResponse, summary="Liveness probe")
async def liveness(
    container: Annotated[ServiceContainer, Depends(get_container)],
) -> HealthResponse:
    return HealthResponse(
        status="ok",
        service=container.settings.service_name,
        version=SERVICE_VERSION,
        environment=str(container.settings.environment),
        dependencies={},
    )


@router.get("/ready", response_model=HealthResponse, summary="Readiness probe")
async def readiness(
    container: Annotated[ServiceContainer, Depends(get_container)],
    response: Response,
) -> HealthResponse:
    """Check every dependency on the request path, concurrently."""
    checks = {
        "postgres": container.postgres.health_check(),
        "mongo": container.mongo.health_check(),
        "storage": container.storage.health_check(),
        # Neo4j is checked but treated as non-blocking below: Phase 1 does not use the
        # graph on the request path, so its absence must not take the service out of
        # rotation. Reporting it keeps the dependency visible before Phase 2 needs it.
        "neo4j": container.neo4j.health_check(),
    }

    results = await asyncio.gather(
        *(_probe(name, coro) for name, coro in checks.items()), return_exceptions=False
    )
    dependencies: dict[str, Any] = dict(results)

    blocking = {"postgres", "mongo", "storage"}
    degraded = [
        name
        for name, payload in dependencies.items()
        if payload.get("status") != "ok" and name in blocking
    ]

    if degraded:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        _log.warning("health.not_ready", degraded=degraded)

    return HealthResponse(
        status="degraded" if degraded else "ok",
        service=container.settings.service_name,
        version=SERVICE_VERSION,
        environment=str(container.settings.environment),
        dependencies=dependencies,
    )


async def _probe(name: str, coro: Any) -> tuple[str, dict[str, Any]]:
    """Run one dependency check, converting failure and timeout into a payload."""
    try:
        async with asyncio.timeout(_READINESS_TIMEOUT_SECONDS):
            return name, await coro
    except TimeoutError:
        return name, {"status": "timeout"}
    except DependencyUnavailableError as exc:
        return name, {"status": "unavailable", "detail": exc.detail}
    except Exception as exc:
        return name, {"status": "error", "detail": type(exc).__name__}
