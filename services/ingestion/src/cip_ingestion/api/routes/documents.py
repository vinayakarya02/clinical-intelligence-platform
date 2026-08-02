"""Document ingestion and inspection endpoints.

Mirrors the contract in docs/api/openapi.yaml. Two behaviours are worth calling out
because they are easy to get subtly wrong:

* **Duplicates return 409, not 200.** A content-identical re-upload is reported as a
  conflict carrying the existing document id, so a client can adopt the existing document.
  Returning 200 would make "already ingested" indistinguishable from "ingested now", and
  clients would double-count.
* **A quarantined document is a successful request.** The upload was accepted, stored, and
  assessed; the quality gate withheld it from retrieval. That is a 201 with a
  ``quarantined`` status and the failing checks attached, not an error — the caller did
  nothing wrong and the document is recoverable.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile, status

from cip_core.errors import NotFoundError, PayloadTooLargeError, ValidationFailedError
from cip_core.logging import get_logger
from cip_core.models.enums import DeidentificationStatus, DocumentType, IngestionStatus
from cip_ingestion.api.dependencies import CurrentContext, ServiceContainer, get_container
from cip_ingestion.api.schemas import (
    ChunkSummary,
    DocumentDetailResponse,
    DocumentListResponse,
    DocumentSummary,
    IngestDocumentResponse,
    IngestionRunSummary,
    QualityReportResponse,
)
from cip_ingestion.pipeline import IngestionRequest
from cip_ingestion.repositories import (
    ChunkRepository,
    DocumentRepository,
    IngestionRunRepository,
    QualityReportRepository,
)

__all__ = ["router"]

_log = get_logger(__name__)

router = APIRouter(prefix="/v1/documents", tags=["Ingestion"])


@router.post(
    "",
    response_model=IngestDocumentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload and ingest a clinical document",
)
async def ingest_document(
    context: CurrentContext,
    container: Annotated[ServiceContainer, Depends(get_container)],
    response: Response,
    file: Annotated[UploadFile, File(description="Document payload (PDF, DOCX, or text)")],
    source_system: Annotated[str, Form(description="Originating system identifier")],
    document_type: Annotated[DocumentType | None, Form()] = None,
    patient_id: Annotated[uuid.UUID | None, Form()] = None,
    deidentification_status: Annotated[
        DeidentificationStatus, Form()
    ] = DeidentificationStatus.NOT_DEIDENTIFIED,
    force_reingest: Annotated[bool, Form()] = False,
) -> IngestDocumentResponse:
    """Ingest a document through the full pipeline."""
    if not source_system.strip():
        raise ValidationFailedError("source_system must not be empty")

    limit = container.settings.ingestion.max_upload_bytes
    # Reject on the declared size before reading the body when the client provided one,
    # so an oversized upload does not have to be buffered in full to be refused.
    if file.size is not None and file.size > limit:
        raise PayloadTooLargeError(f"Payload is {file.size} bytes; the limit is {limit} bytes")

    data = await file.read()
    if len(data) > limit:
        raise PayloadTooLargeError(f"Payload is {len(data)} bytes; the limit is {limit} bytes")

    result = await container.pipeline.ingest(
        IngestionRequest(
            data=data,
            source_system=source_system.strip(),
            filename=file.filename,
            declared_media_type=file.content_type or "application/octet-stream",
            document_type=document_type,
            patient_id=patient_id,
            deidentification_status=deidentification_status,
            force_reingest=force_reingest,
        ),
        context=context,
    )

    if result.status is IngestionStatus.QUARANTINED:
        # 202: accepted and stored, but withheld from retrieval pending review.
        response.status_code = status.HTTP_202_ACCEPTED

    return IngestDocumentResponse(
        document_id=result.document_id,
        status=result.status,
        chunk_count=result.chunk_count,
        content_hash=result.content_hash,
        run_id=result.run_id,
        quality=(
            QualityReportResponse.from_report_json(result.quality.to_json())
            if result.quality is not None
            else None
        ),
        stage_durations_ms=result.stage_durations_ms,
    )


@router.get("", response_model=DocumentListResponse, summary="List documents")
async def list_documents(
    context: CurrentContext,
    container: Annotated[ServiceContainer, Depends(get_container)],
    ingestion_status: Annotated[IngestionStatus | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> DocumentListResponse:
    context.require_scope("documents:read")
    async with container.postgres.tenant_session(context) as session:
        documents = await DocumentRepository(session).list_for_tenant(
            context=context, status=ingestion_status, limit=limit, offset=offset
        )
        items = [DocumentSummary.model_validate(document) for document in documents]
    return DocumentListResponse(items=items, limit=limit, offset=offset)


@router.get(
    "/{document_id}",
    response_model=DocumentDetailResponse,
    summary="Get a document with its chunk metadata, runs, and quality report",
)
async def get_document(
    document_id: uuid.UUID,
    context: CurrentContext,
    container: Annotated[ServiceContainer, Depends(get_container)],
) -> DocumentDetailResponse:
    context.require_scope("documents:read")
    async with container.postgres.tenant_session(context) as session:
        document = await DocumentRepository(session).get(document_id, context=context)
        if document is None:
            # 404 rather than 403 for a document in another tenant: a 403 would confirm
            # the id exists, turning this endpoint into a cross-tenant existence oracle.
            raise NotFoundError(f"Document {document_id} was not found")

        chunks = await ChunkRepository(session).list_for_document(document_id, context=context)
        runs = await IngestionRunRepository(session).list_for_document(document_id, context=context)
        quality_row = await QualityReportRepository(session).latest_for_document(
            document_id, context=context
        )

        return DocumentDetailResponse(
            document=DocumentSummary.model_validate(document),
            metadata=document.doc_metadata or {},
            chunks=[ChunkSummary.model_validate(chunk) for chunk in chunks],
            runs=[IngestionRunSummary.model_validate(run) for run in runs],
            quality=(
                QualityReportResponse.from_report_json(quality_row.checks)
                if quality_row is not None
                else None
            ),
        )


@router.delete(
    "/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Soft-delete a document and schedule it for purge",
)
async def delete_document(
    document_id: uuid.UUID,
    context: CurrentContext,
    container: Annotated[ServiceContainer, Depends(get_container)],
) -> Response:
    context.require_scope("documents:write")
    async with container.postgres.tenant_session(context) as session:
        deleted = await DocumentRepository(session).soft_delete(document_id, context=context)
    if not deleted:
        raise NotFoundError(f"Document {document_id} was not found")
    _log.info("document.soft_deleted", document_id=str(document_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
