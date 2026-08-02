"""API request and response models.

Separate from the ORM models on purpose. The wire contract in docs/api/openapi.yaml and
the database schema change for different reasons and at different rates, and coupling them
means a column rename becomes a breaking API change. These models also enforce the
boundary that matters most here: no response model exposes ``chunk_text``, because chunk
content is PHI and the ingestion API's job is to report *status*, not to serve documents.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from cip_core.models.enums import (
    DeidentificationStatus,
    DocumentType,
    IngestionStatus,
    QualityVerdict,
)

__all__ = [
    "ChunkSummary",
    "DocumentDetailResponse",
    "DocumentListResponse",
    "DocumentSummary",
    "HealthResponse",
    "IngestDocumentResponse",
    "IngestionRunSummary",
    "QualityCheckSummary",
    "QualityReportResponse",
]


class _Base(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")


class QualityCheckSummary(_Base):
    name: str
    passed: bool
    score: float
    detail: str


class QualityReportResponse(_Base):
    """Client-facing view of a quality report.

    Deliberately a projection of the stored report rather than a pass-through: check
    ``weight`` and ``observed`` are internal tuning detail that would become part of the
    API contract if exposed, and re-weighting a check should not be a breaking API change.
    The full detail remains queryable in ``document_quality_reports.checks``.
    """

    verdict: QualityVerdict
    score: float = Field(ge=0.0, le=1.0)
    checks: list[QualityCheckSummary] = Field(default_factory=list)
    failed_checks: list[str] = Field(default_factory=list)

    @classmethod
    def from_report_json(cls, payload: dict[str, Any]) -> QualityReportResponse:
        """Project a stored/serialised quality report onto the response model."""
        return cls(
            verdict=QualityVerdict(payload["verdict"]),
            score=float(payload["score"]),
            checks=[
                QualityCheckSummary(
                    name=check["name"],
                    passed=bool(check["passed"]),
                    score=float(check["score"]),
                    detail=check["detail"],
                )
                for check in payload.get("checks", [])
            ],
            failed_checks=list(payload.get("failed_checks", [])),
        )


class IngestDocumentResponse(_Base):
    """Result of an ingestion request."""

    document_id: uuid.UUID
    status: IngestionStatus
    chunk_count: int
    content_hash: str
    run_id: uuid.UUID | None = None
    quality: QualityReportResponse | None = None
    stage_durations_ms: dict[str, float] = Field(default_factory=dict)


class DocumentSummary(_Base):
    document_id: uuid.UUID
    document_type: DocumentType
    source_system: str
    title: str | None = None
    original_filename: str | None = None
    media_type: str
    size_bytes: int
    content_hash: str
    ingestion_status: IngestionStatus
    deidentification_status: DeidentificationStatus
    page_count: int | None = None
    language: str | None = None
    effective_date: dt.date | None = None
    failure_reason: str | None = None
    created_at: dt.datetime


class ChunkSummary(_Base):
    """Chunk metadata without its text.

    ``chunk_text`` is deliberately absent — see the module docstring.
    """

    chunk_id: uuid.UUID
    chunk_index: int
    section_type: str
    section_heading: str | None = None
    token_count: int
    char_start: int
    char_end: int
    page_start: int | None = None
    page_end: int | None = None
    content_hash: str


class IngestionRunSummary(_Base):
    run_id: uuid.UUID
    status: str
    parser_name: str | None = None
    pipeline_version: str
    chunk_count: int
    failed_stage: str | None = None
    failure_reason: str | None = None
    stage_durations_ms: dict[str, float] = Field(default_factory=dict)
    started_at: dt.datetime
    completed_at: dt.datetime | None = None


class DocumentDetailResponse(_Base):
    document: DocumentSummary
    metadata: dict[str, Any] = Field(default_factory=dict)
    chunks: list[ChunkSummary] = Field(default_factory=list)
    runs: list[IngestionRunSummary] = Field(default_factory=list)
    quality: QualityReportResponse | None = None


class DocumentListResponse(_Base):
    items: list[DocumentSummary] = Field(default_factory=list)
    limit: int
    offset: int


class HealthResponse(_Base):
    status: str
    service: str
    version: str
    environment: str
    dependencies: dict[str, Any] = Field(default_factory=dict)
