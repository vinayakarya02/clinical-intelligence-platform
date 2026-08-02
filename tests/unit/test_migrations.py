"""Migration tests.

These run Alembic in offline mode, which needs no database but does exercise the parts
that break silently: that ``env.py`` can import the application package, that every
revision is syntactically valid, and that the generated DDL contains what the design
requires.

The import check exists because of a real regression — ``prepend_sys_path`` in
``alembic.ini`` was joined with ``:`` while ``path_separator`` was ``os``, so on Windows
the paths were never split and ``alembic`` could not import ``cip_core`` at all. Tests
passed throughout, because nothing else invoked Alembic as a subprocess.
"""

from __future__ import annotations

import io
import re
from contextlib import redirect_stdout
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def alembic_config() -> Config:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    return config


@pytest.fixture
def offline_sql(alembic_config: Config) -> str:
    """Render the full upgrade as SQL without touching a database."""
    from alembic import command

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        command.upgrade(alembic_config, "head", sql=True)
    return buffer.getvalue()


class TestRevisionGraph:
    def test_revisions_load(self, alembic_config: Config) -> None:
        """Proves env.py imports cleanly and every revision file parses."""
        script = ScriptDirectory.from_config(alembic_config)
        revisions = list(script.walk_revisions())
        assert revisions

    def test_there_is_exactly_one_head(self, alembic_config: Config) -> None:
        """Multiple heads mean a merge was missed and `upgrade head` becomes ambiguous."""
        script = ScriptDirectory.from_config(alembic_config)
        assert len(script.get_heads()) == 1

    def test_base_revision_is_reachable(self, alembic_config: Config) -> None:
        script = ScriptDirectory.from_config(alembic_config)
        head = script.get_current_head()
        assert head is not None
        assert list(script.iterate_revisions(head, "base"))


class TestGeneratedSchema:
    @pytest.mark.parametrize(
        "table",
        [
            "platform.tenants",
            "platform.audit_log",
            "documents",
            "document_chunks",
            "ingestion_runs",
            "document_quality_reports",
            "index_sync_state",
        ],
    )
    def test_every_phase1_table_is_created(self, offline_sql: str, table: str) -> None:
        assert f"CREATE TABLE {table}" in offline_sql

    @pytest.mark.parametrize(
        "table",
        ["documents", "document_chunks", "ingestion_runs", "document_quality_reports"],
    )
    def test_tenant_scoped_tables_get_an_rls_policy(self, offline_sql: str, table: str) -> None:
        """RLS is the database-enforced floor of tenant isolation (ADR-0003)."""
        assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" in offline_sql
        assert f"CREATE POLICY tenant_isolation_{table} ON {table}" in offline_sql

    def test_policies_constrain_writes_as_well_as_reads(self, offline_sql: str) -> None:
        """Without WITH CHECK, a caller could insert rows it can never read back."""
        policies = re.findall(r"CREATE POLICY .*?;", offline_sql, flags=re.DOTALL)
        assert policies
        for policy in policies:
            assert "USING" in policy
            assert "WITH CHECK" in policy

    def test_rls_is_forced_so_table_owners_are_not_exempt(self, offline_sql: str) -> None:
        """Postgres exempts table owners from RLS unless FORCE is set."""
        assert offline_sql.count("FORCE ROW LEVEL SECURITY") >= 4

    def test_audit_log_carries_the_hash_chain_columns(self, offline_sql: str) -> None:
        assert "prev_hash" in offline_sql
        assert "row_hash" in offline_sql

    def test_documents_have_a_content_addressed_uniqueness_constraint(
        self, offline_sql: str
    ) -> None:
        assert "uq_documents_tenant_source_content_hash" in offline_sql

    def test_tenant_id_is_indexed_on_tenant_scoped_tables(self, offline_sql: str) -> None:
        """Phase 0 review finding D4/D8: an unindexed tenant_id makes RLS a seq scan."""
        for index in (
            "ix_documents_tenant_id",
            "ix_document_chunks_tenant_id",
            "ix_ingestion_runs_tenant_id",
        ):
            assert index in offline_sql

    def test_soft_delete_and_purge_columns_exist(self, offline_sql: str) -> None:
        """Phase 0 review finding D3: retention/purge needs these columns."""
        assert "deleted_at" in offline_sql
        assert "purge_after" in offline_sql
