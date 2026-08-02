"""CLI tests.

Exit codes get explicit coverage because the CLI runs unattended in cron and CI, where a
wrong code is the difference between a silent partial failure and a caught one.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from cip_ingestion.cli import (
    EXIT_OK,
    EXIT_PARTIAL_FAILURE,
    EXIT_USAGE,
    _discover_files,
    _report,
    build_parser,
    main,
)
from tests.fixtures.documents import build_pdf


class TestArgumentParsing:
    def test_ingest_requires_tenant_and_source(self) -> None:
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["ingest", "file.txt"])

    def test_ingest_parses_full_arguments(self) -> None:
        tenant = uuid.uuid4()
        args = build_parser().parse_args(
            [
                "ingest",
                "a.txt",
                "b.pdf",
                "--tenant-id",
                str(tenant),
                "--source-system",
                "epic",
                "--document-type",
                "lab_report",
                "--force",
                "--recursive",
            ]
        )
        assert args.tenant_id == tenant
        assert args.source_system == "epic"
        assert str(args.document_type) == "lab_report"
        assert args.force is True
        assert args.recursive is True

    def test_rejects_an_unknown_document_type(self) -> None:
        with pytest.raises(SystemExit):
            build_parser().parse_args(
                [
                    "ingest",
                    "a.txt",
                    "--tenant-id",
                    str(uuid.uuid4()),
                    "--source-system",
                    "epic",
                    "--document-type",
                    "not-a-real-type",
                ]
            )

    def test_a_subcommand_is_required(self) -> None:
        with pytest.raises(SystemExit):
            build_parser().parse_args([])

    @pytest.mark.parametrize("command", ["health", "config", "db"])
    def test_other_subcommands_parse(self, command: str) -> None:
        argv = [command] if command != "db" else ["db", "upgrade"]
        assert build_parser().parse_args(argv).command == command


class TestFileDiscovery:
    def test_finds_a_single_file(self, tmp_path: Path) -> None:
        target = tmp_path / "note.txt"
        target.write_bytes(b"content")
        assert _discover_files([target], recursive=False) == [target]

    def test_finds_supported_files_in_a_directory(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_bytes(b"a")
        (tmp_path / "b.pdf").write_bytes(build_pdf("b"))
        (tmp_path / "ignored.png").write_bytes(b"png")

        found = _discover_files([tmp_path], recursive=False)
        assert {path.name for path in found} == {"a.txt", "b.pdf"}

    def test_recursive_discovery_descends(self, tmp_path: Path) -> None:
        nested = tmp_path / "sub" / "deeper"
        nested.mkdir(parents=True)
        (nested / "note.txt").write_bytes(b"x")

        assert _discover_files([tmp_path], recursive=False) == []
        assert len(_discover_files([tmp_path], recursive=True)) == 1

    def test_results_are_sorted_and_deduplicated(self, tmp_path: Path) -> None:
        """A reproducible batch order makes failure reports diffable."""
        for name in ("c.txt", "a.txt", "b.txt"):
            (tmp_path / name).write_bytes(b"x")

        found = _discover_files([tmp_path, tmp_path], recursive=False)
        assert [path.name for path in found] == ["a.txt", "b.txt", "c.txt"]

    def test_missing_path_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            _discover_files([tmp_path / "absent.txt"], recursive=False)


class TestReporting:
    def test_json_output_is_machine_readable(self, capsys: pytest.CaptureFixture[str]) -> None:
        _report(
            [
                {
                    "path": "a.txt",
                    "outcome": "ingested",
                    "document_id": "d1",
                    "status": "chunked",
                    "chunk_count": 4,
                    "quality_verdict": "pass",
                    "quality_score": 0.95,
                }
            ],
            as_json=True,
        )
        payload = json.loads(capsys.readouterr().out)
        assert payload["results"][0]["document_id"] == "d1"

    def test_human_output_summarises_outcomes(self, capsys: pytest.CaptureFixture[str]) -> None:
        _report(
            [
                {
                    "path": "a.txt",
                    "outcome": "ingested",
                    "document_id": "d1",
                    "status": "chunked",
                    "chunk_count": 4,
                    "quality_verdict": "pass",
                },
                {"path": "b.txt", "outcome": "duplicate", "document_id": "d2"},
                {"path": "c.txt", "outcome": "failed", "detail": "broken"},
            ],
            as_json=False,
        )
        captured = capsys.readouterr()
        assert "1 ingested, 1 duplicate, 1 failed" in captured.out
        assert "broken" in captured.err


class TestCommands:
    def test_config_command_succeeds(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["config", "--json"]) == EXIT_OK
        payload = json.loads(capsys.readouterr().out)
        assert payload["environment"]
        assert payload["pipeline_version"]

    def test_config_output_contains_no_secrets(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CIP_POSTGRES__PASSWORD", "super-secret-value")
        from cip_core.config import get_settings

        get_settings.cache_clear()

        main(["config", "--json"])
        assert "super-secret-value" not in capsys.readouterr().out

    def test_ingest_with_no_matching_files_is_a_usage_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert (
            main(
                [
                    "ingest",
                    str(tmp_path),
                    "--tenant-id",
                    str(uuid.uuid4()),
                    "--source-system",
                    "epic",
                ]
            )
            == EXIT_USAGE
        )
        assert "No ingestible files" in capsys.readouterr().err

    def test_ingest_of_a_missing_path_is_a_usage_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert (
            main(
                [
                    "ingest",
                    str(tmp_path / "nope.txt"),
                    "--tenant-id",
                    str(uuid.uuid4()),
                    "--source-system",
                    "epic",
                ]
            )
            == EXIT_USAGE
        )
        assert "error:" in capsys.readouterr().err


class TestExitCodes:
    def test_exit_codes_are_distinct(self) -> None:
        from cip_ingestion.cli import EXIT_DEPENDENCY

        assert len({EXIT_OK, EXIT_PARTIAL_FAILURE, EXIT_USAGE, EXIT_DEPENDENCY}) == 4

    def test_success_is_zero(self) -> None:
        assert EXIT_OK == 0
