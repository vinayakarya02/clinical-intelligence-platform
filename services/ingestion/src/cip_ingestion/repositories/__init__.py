"""Persistence layer.

Repositories own all database access. Pipeline and API code never issues queries directly,
which keeps the tenant-scoping rule (ADR-0003) enforceable in one place instead of at
every call site.
"""

from cip_ingestion.repositories.artifacts import ParsedArtifactRepository
from cip_ingestion.repositories.audit import AuditRepository
from cip_ingestion.repositories.documents import ChunkRepository, DocumentRepository
from cip_ingestion.repositories.runs import (
    IngestionRunRepository,
    QualityReportRepository,
    SyncStateRepository,
)

__all__ = [
    "AuditRepository",
    "ChunkRepository",
    "DocumentRepository",
    "IngestionRunRepository",
    "ParsedArtifactRepository",
    "QualityReportRepository",
    "SyncStateRepository",
]
