"""SQLAlchemy declarative base and shared column conventions."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

__all__ = ["Base", "utcnow"]

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
