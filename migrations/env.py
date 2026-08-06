"""Alembic environment.

The database URL comes from application settings rather than ``alembic.ini`` so there is
exactly one source of truth for connection details and no credential in a committed file.

**Unless the caller supplied one.** Until Phase 9 W6 the settings URL was written
unconditionally, which silently *overwrote* anything a caller had set — so there was no way to
point Alembic at a different database at all. `alembic -x`, a restored replica, a dry run against
a copy: none of them worked, and none of them failed either. They ran against production's
database while appearing to run against the target.

The migration tests found it the expensive way. `tests/integration/test_migrations.py` builds a
throwaway database per test and sets the URL to it; the override was discarded, so every test ran
against the **shared CI database** — including ``downgrade base``, which dropped the real schema
out from under every test that followed. That is the same class of defect as the fixture teardown
W6 was written to remove, reintroduced through a different door: something that looked like it
targeted a scratch database and did not.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from cip_core.config import get_settings
from cip_core.db.base import Base
from cip_core.models import tables as _tables  # noqa: F401  (import registers the models)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# An explicit URL wins. Settings are the default, not an override: a caller that went to the
# trouble of naming a database meant it, and quietly redirecting them to another one is worse
# than refusing.
if not config.get_main_option("sqlalchemy.url", ""):
    config.set_main_option("sqlalchemy.url", get_settings().postgres.dsn())


def _include_object(obj: object, name: str | None, type_: str, reflected: bool, _compare) -> bool:  # type: ignore[no-untyped-def]
    """Keep autogenerate scoped to tables this application owns.

    Without this, running autogenerate against a database that also hosts tables managed
    elsewhere (or the clinical-fact tables that arrive in a later phase) produces
    destructive ``DROP TABLE`` operations in the generated revision.
    """
    if type_ == "table" and reflected and name is not None:
        return name in target_metadata.tables or f"platform.{name}" in target_metadata.tables
    return True


def run_migrations_offline() -> None:
    """Emit SQL without a live connection (``alembic upgrade head --sql``)."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_schemas=True,
        include_object=_include_object,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_schemas=True,
        include_object=_include_object,
        compare_type=True,
        version_table_schema="public",
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(_run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
