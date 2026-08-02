"""SQLAlchemy declarative base and shared column conventions."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated

from sqlalchemy import DateTime, MetaData, Uuid, func
from sqlalchemy.orm import DeclarativeBase, mapped_column

__all__ = ["Base", "created_at_column", "primary_uuid_column", "tenant_id_column", "utcnow"]

# Explicit naming convention so Alembic autogenerate produces stable, reviewable
# constraint names instead of database-assigned ones that differ per environment.
_NAMING_CONVENTION = {
    "ix": "ix_%(column_0_N_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""

    metadata = MetaData(naming_convention=_NAMING_CONVENTION)


def utcnow() -> dt.datetime:
    """Timezone-aware current UTC time.

    Used for application-side timestamps. Database-side defaults use ``now()`` so rows
    written outside the application (migrations, admin scripts) are still stamped.
    """
    return dt.datetime.now(dt.UTC)


UuidPk = Annotated[uuid.UUID, mapped_column(Uuid(as_uuid=True), primary_key=True)]


def primary_uuid_column() -> object:
    return mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)


def tenant_id_column(*, index: bool = True) -> object:
    """Tenant discriminator.

    Present on every tenant-scoped table even under schema-per-tenant isolation, and
    always the leading column of an index — the Phase 0 review found RLS predicates
    running as sequential scans without it (finding D4/D8).
    """
    return mapped_column(Uuid(as_uuid=True), nullable=False, index=index)


def created_at_column() -> object:
    return mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), default=utcnow
    )
