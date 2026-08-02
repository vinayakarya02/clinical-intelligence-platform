"""HTTP API tests.

These drive the real application — real middleware, real auth, real pipeline, real
database — with only MongoDB and object storage substituted. An API test that stubbed the
pipeline would assert nothing about whether an upload actually works.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from jose import jwt

from cip_core.config import Settings
from cip_core.db.neo4j import Neo4jManager
from cip_core.db.postgres import PostgresManager
from cip_core.models import IngestionStatus
from cip_ingestion.api.app import create_app
from cip_ingestion.api.dependencies import ServiceContainer
from cip_ingestion.api.security import build_verifier
from cip_ingestion.parsers import build_parser_registry
from cip_ingestion.pipeline import IngestionPipeline
from cip_ingestion.processor import DocumentProcessor
from tests.fakes import FakeMongoManager, InMemoryStorage
from tests.fixtures.documents import DISCHARGE_SUMMARY_TEXT, build_pdf

_TENANT_CLAIM = "https://cip.example.com/tenant_id"
_ROLES_CLAIM = "https://cip.example.com/roles"

PROBLEM_JSON = "application/problem+json"


@pytest.fixture
def container(
    settings: Settings,
    postgres: PostgresManager,
    mongo: FakeMongoManager,
) -> ServiceContainer:
    """A container wired to the in-memory database and fake externals."""
    storage = InMemoryStorage()
    processor = DocumentProcessor(
        parsers=build_parser_registry(settings.ingestion), settings=settings.ingestion
    )
    return ServiceContainer(
        settings=settings,
        postgres=postgres,
        mongo=mongo,  # type: ignore[arg-type]
        neo4j=Neo4jManager(settings.neo4j),
        storage=storage,
        pipeline=IngestionPipeline(
            settings=settings,
            postgres=postgres,
            mongo=mongo,  # type: ignore[arg-type]
            storage=storage,
            processor=processor,
        ),
        verifier=build_verifier(settings.auth),
    )


@pytest.fixture
def app(settings: Settings, container: ServiceContainer) -> FastAPI:
    return create_app(settings, container=container)


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """Client that skips lifespan, since connections are already wired by the fixtures."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        yield client


def _auth_header(
    settings: Settings,
    tenant_id: uuid.UUID,
    *,
    scope: str = "documents:read documents:write",
    roles: list[str] | None = None,
) -> dict[str, str]:
    now = dt.datetime.now(dt.UTC)
    token = jwt.encode(
        {
            "iss": settings.auth.jwt_issuer,
            "aud": settings.auth.jwt_audience,
            "sub": "api-test-user",
            "iat": now,
            "exp": now + dt.timedelta(minutes=5),
            "scope": scope,
            _TENANT_CLAIM: str(tenant_id),
            _ROLES_CLAIM: roles or ["clinician"],
        },
        settings.auth.jwt_secret.get_secret_value(),
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


def _upload(name: str = "discharge.txt", content: bytes | None = None) -> dict:
    return {"file": (name, content or DISCHARGE_SUMMARY_TEXT.encode(), "text/plain")}


class TestHealthEndpoints:
    async def test_liveness_needs_no_authentication(self, client: AsyncClient) -> None:
        response = await client.get("/health/live")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    async def test_readiness_reports_dependency_state(self, client: AsyncClient) -> None:
        response = await client.get("/health/ready")
        body = response.json()
        assert set(body["dependencies"]) == {"postgres", "mongo", "storage", "neo4j"}
        assert body["dependencies"]["mongo"]["status"] == "ok"

    async def test_readiness_tolerates_an_unreachable_neo4j(self, client: AsyncClient) -> None:
        """Phase 1 does not use the graph on the request path, so it must not block traffic."""
        response = await client.get("/health/ready")
        assert response.status_code == 200
        assert response.json()["dependencies"]["neo4j"]["status"] != "ok"

    async def test_request_id_is_echoed(self, client: AsyncClient) -> None:
        response = await client.get("/health/live", headers={"X-Request-ID": "trace-me"})
        assert response.headers["X-Request-ID"] == "trace-me"

    async def test_request_id_is_generated_when_absent(self, client: AsyncClient) -> None:
        response = await client.get("/health/live")
        assert uuid.UUID(response.headers["X-Request-ID"])


class TestAuthentication:
    async def test_upload_requires_a_token(self, client: AsyncClient) -> None:
        response = await client.post(
            "/v1/documents", files=_upload(), data={"source_system": "epic"}
        )
        assert response.status_code == 401
        assert response.headers["content-type"].startswith(PROBLEM_JSON)

    async def test_malformed_authorization_header_is_rejected(self, client: AsyncClient) -> None:
        response = await client.post(
            "/v1/documents",
            files=_upload(),
            data={"source_system": "epic"},
            headers={"Authorization": "Basic abc123"},
        )
        assert response.status_code == 401

    async def test_invalid_token_is_rejected(self, client: AsyncClient) -> None:
        response = await client.get("/v1/documents", headers={"Authorization": "Bearer not.a.jwt"})
        assert response.status_code == 401

    async def test_missing_scope_is_rejected(
        self, client: AsyncClient, settings: Settings, tenant_id: uuid.UUID
    ) -> None:
        response = await client.post(
            "/v1/documents",
            files=_upload(),
            data={"source_system": "epic"},
            headers=_auth_header(settings, tenant_id, scope="documents:read"),
        )
        assert response.status_code == 403
        assert "documents:write" in response.json()["detail"]


class TestDocumentIngestion:
    async def test_uploads_and_ingests_a_document(
        self, client: AsyncClient, settings: Settings, tenant_id: uuid.UUID
    ) -> None:
        response = await client.post(
            "/v1/documents",
            files=_upload(),
            data={"source_system": "epic"},
            headers=_auth_header(settings, tenant_id),
        )
        assert response.status_code == 201

        body = response.json()
        assert body["status"] == IngestionStatus.CHUNKED.value
        assert body["chunk_count"] > 0
        assert body["quality"]["verdict"] == "pass"
        assert len(body["content_hash"]) == 64
        assert body["stage_durations_ms"]

    async def test_uploads_a_pdf(
        self, client: AsyncClient, settings: Settings, tenant_id: uuid.UUID
    ) -> None:
        response = await client.post(
            "/v1/documents",
            files={"file": ("note.pdf", build_pdf(DISCHARGE_SUMMARY_TEXT), "application/pdf")},
            data={"source_system": "epic"},
            headers=_auth_header(settings, tenant_id),
        )
        assert response.status_code == 201
        assert response.json()["chunk_count"] > 0

    async def test_duplicate_upload_returns_conflict_with_the_existing_id(
        self, client: AsyncClient, settings: Settings, tenant_id: uuid.UUID
    ) -> None:
        headers = _auth_header(settings, tenant_id)
        first = await client.post(
            "/v1/documents", files=_upload(), data={"source_system": "epic"}, headers=headers
        )
        second = await client.post(
            "/v1/documents", files=_upload(), data={"source_system": "epic"}, headers=headers
        )

        assert second.status_code == 409
        assert second.json()["existing_document_id"] == first.json()["document_id"]

    async def test_quarantined_document_is_accepted_not_rejected(
        self, client: AsyncClient, settings: Settings, tenant_id: uuid.UUID
    ) -> None:
        """The caller did nothing wrong; the gate withheld the document from retrieval."""
        garbled = (chr(0xFFFD) * 500).encode("utf-8")
        response = await client.post(
            "/v1/documents",
            files=_upload("garbled.txt", garbled),
            data={"source_system": "epic"},
            headers=_auth_header(settings, tenant_id),
        )
        assert response.status_code == 202
        body = response.json()
        assert body["status"] == IngestionStatus.QUARANTINED.value
        assert body["quality"]["verdict"] == "fail"
        assert body["quality"]["failed_checks"]

    async def test_unsupported_media_type_is_rejected(
        self, client: AsyncClient, settings: Settings, tenant_id: uuid.UUID
    ) -> None:
        response = await client.post(
            "/v1/documents",
            files={"file": ("image.png", bytes([0x89, 0x50, 0x4E, 0x47, 0x00]), "image/png")},
            data={"source_system": "epic"},
            headers=_auth_header(settings, tenant_id),
        )
        assert response.status_code == 415
        assert response.headers["content-type"].startswith(PROBLEM_JSON)

    async def test_empty_source_system_is_rejected(
        self, client: AsyncClient, settings: Settings, tenant_id: uuid.UUID
    ) -> None:
        response = await client.post(
            "/v1/documents",
            files=_upload(),
            data={"source_system": "   "},
            headers=_auth_header(settings, tenant_id),
        )
        assert response.status_code == 422

    async def test_oversized_payload_is_rejected(
        self, client: AsyncClient, settings: Settings, tenant_id: uuid.UUID, app: FastAPI
    ) -> None:
        app.state.container.settings.ingestion.max_upload_bytes = 128
        response = await client.post(
            "/v1/documents",
            files=_upload("big.txt", b"x" * 4096),
            data={"source_system": "epic"},
            headers=_auth_header(settings, tenant_id),
        )
        assert response.status_code == 413

    async def test_declared_document_type_is_honoured(
        self, client: AsyncClient, settings: Settings, tenant_id: uuid.UUID
    ) -> None:
        response = await client.post(
            "/v1/documents",
            files=_upload(),
            data={"source_system": "epic", "document_type": "trial_protocol"},
            headers=_auth_header(settings, tenant_id),
        )
        assert response.status_code == 201

        detail = await client.get(
            f"/v1/documents/{response.json()['document_id']}",
            headers=_auth_header(settings, tenant_id),
        )
        assert detail.json()["document"]["document_type"] == "trial_protocol"


class TestDocumentRetrieval:
    async def _ingest(self, client: AsyncClient, settings: Settings, tenant_id: uuid.UUID) -> str:
        response = await client.post(
            "/v1/documents",
            files=_upload(),
            data={"source_system": "epic"},
            headers=_auth_header(settings, tenant_id),
        )
        return str(response.json()["document_id"])

    async def test_lists_documents(
        self, client: AsyncClient, settings: Settings, tenant_id: uuid.UUID
    ) -> None:
        await self._ingest(client, settings, tenant_id)
        response = await client.get("/v1/documents", headers=_auth_header(settings, tenant_id))
        assert response.status_code == 200
        assert len(response.json()["items"]) == 1

    async def test_document_detail_includes_chunks_runs_and_quality(
        self, client: AsyncClient, settings: Settings, tenant_id: uuid.UUID
    ) -> None:
        document_id = await self._ingest(client, settings, tenant_id)
        response = await client.get(
            f"/v1/documents/{document_id}", headers=_auth_header(settings, tenant_id)
        )
        body = response.json()

        assert body["document"]["document_id"] == document_id
        assert body["chunks"]
        assert body["runs"][0]["parser_name"] == "text"
        assert body["quality"]["verdict"] == "pass"
        assert body["metadata"]["section_names"]

    async def test_chunk_text_is_never_returned(
        self, client: AsyncClient, settings: Settings, tenant_id: uuid.UUID
    ) -> None:
        """Chunk content is PHI; this API reports status, it does not serve documents."""
        document_id = await self._ingest(client, settings, tenant_id)
        response = await client.get(
            f"/v1/documents/{document_id}", headers=_auth_header(settings, tenant_id)
        )
        assert "Substernal chest pain" not in response.text
        assert all("chunk_text" not in chunk for chunk in response.json()["chunks"])

    async def test_another_tenant_gets_404_not_403(
        self,
        client: AsyncClient,
        settings: Settings,
        tenant_id: uuid.UUID,
        other_tenant_id: uuid.UUID,
    ) -> None:
        """403 would confirm the id exists, making this a cross-tenant existence oracle."""
        document_id = await self._ingest(client, settings, tenant_id)
        response = await client.get(
            f"/v1/documents/{document_id}", headers=_auth_header(settings, other_tenant_id)
        )
        assert response.status_code == 404

    async def test_unknown_document_returns_404(
        self, client: AsyncClient, settings: Settings, tenant_id: uuid.UUID
    ) -> None:
        response = await client.get(
            f"/v1/documents/{uuid.uuid4()}", headers=_auth_header(settings, tenant_id)
        )
        assert response.status_code == 404

    async def test_soft_delete_removes_the_document_from_view(
        self, client: AsyncClient, settings: Settings, tenant_id: uuid.UUID
    ) -> None:
        document_id = await self._ingest(client, settings, tenant_id)
        headers = _auth_header(settings, tenant_id)

        deleted = await client.delete(f"/v1/documents/{document_id}", headers=headers)
        assert deleted.status_code == 204

        gone = await client.get(f"/v1/documents/{document_id}", headers=headers)
        assert gone.status_code == 404

        again = await client.delete(f"/v1/documents/{document_id}", headers=headers)
        assert again.status_code == 404


class TestErrorFormat:
    async def test_errors_use_rfc7807_problem_details(
        self, client: AsyncClient, settings: Settings, tenant_id: uuid.UUID
    ) -> None:
        response = await client.get(
            f"/v1/documents/{uuid.uuid4()}", headers=_auth_header(settings, tenant_id)
        )
        body = response.json()

        assert response.headers["content-type"].startswith(PROBLEM_JSON)
        assert set(body) >= {"type", "title", "status", "detail"}
        assert body["status"] == 404
        assert body["type"].startswith("https://")
