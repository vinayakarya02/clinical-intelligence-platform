"""Command-line interface.

Batch ingestion is not a lesser path than the API — it is how a hospital onboards a
corpus, and how the pipeline is re-run after a version bump. It therefore uses exactly the
same :class:`~cip_ingestion.pipeline.IngestionPipeline` as the HTTP route, with a
service-role :class:`~cip_core.tenancy.TenantContext` in place of a bearer token.

``argparse`` rather than a CLI framework: the command surface is small and stable, and
this keeps the dependency footprint of the deployed image smaller.

Exit codes are meaningful because this runs unattended in cron and CI:
0 success, 1 partial failure (some documents failed), 2 usage error, 3 dependency
unavailable. A batch that ingests 99 of 100 documents exits 1 — it neither passes silently
nor looks like a total failure.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from cip_core.config import Settings, get_settings
from cip_core.errors import CipError, DependencyUnavailableError, DuplicateDocumentError
from cip_core.logging import configure_logging, get_logger
from cip_core.models.enums import DeidentificationStatus, DocumentType
from cip_core.tenancy import TenantContext
from cip_ingestion.api.dependencies import ServiceContainer
from cip_ingestion.pipeline import IngestionRequest
from cip_ingestion.validation import MEDIA_TYPE_EXTENSIONS
from cip_ingestion.version import PIPELINE_VERSION, SERVICE_VERSION

__all__ = ["main"]

_log = get_logger(__name__)

EXIT_OK = 0
EXIT_PARTIAL_FAILURE = 1
EXIT_USAGE = 2
EXIT_DEPENDENCY = 3

_EXTENSION_TO_MEDIA_TYPE = {ext: media for media, ext in MEDIA_TYPE_EXTENSIONS.items()}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cip",
        description="Clinical Intelligence Platform — ingestion service CLI",
    )
    parser.add_argument("--version", action="version", version=f"cip {SERVICE_VERSION}")
    subcommands = parser.add_subparsers(dest="command", required=True)

    ingest = subcommands.add_parser("ingest", help="Ingest one or more documents")
    ingest.add_argument("paths", nargs="+", type=Path, help="Files or directories to ingest")
    ingest.add_argument("--tenant-id", required=True, type=uuid.UUID)
    ingest.add_argument("--source-system", required=True)
    ingest.add_argument("--actor", default="cli", help="Actor id recorded in the audit trail")
    ingest.add_argument(
        "--document-type",
        type=DocumentType,
        choices=list(DocumentType),
        default=None,
        help="Override classification when the source system already knows the type",
    )
    ingest.add_argument("--patient-id", type=uuid.UUID, default=None)
    ingest.add_argument(
        "--deidentification-status",
        type=DeidentificationStatus,
        choices=list(DeidentificationStatus),
        default=DeidentificationStatus.NOT_DEIDENTIFIED,
    )
    ingest.add_argument(
        "--force",
        action="store_true",
        help="Reprocess even if content-identical to an existing document",
    )
    ingest.add_argument("--recursive", action="store_true", help="Descend into directories")
    ingest.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Keep going after a document fails (the default for directories)",
    )
    ingest.add_argument("--json", action="store_true", help="Emit machine-readable results")

    health = subcommands.add_parser("health", help="Check backing-service connectivity")
    health.add_argument("--json", action="store_true")

    config = subcommands.add_parser("config", help="Print the effective configuration")
    config.add_argument("--json", action="store_true")

    db = subcommands.add_parser("db", help="Database schema management")
    db.add_argument("action", choices=["upgrade", "downgrade", "current"])
    db.add_argument("--revision", default="head")

    return parser


def _discover_files(paths: Sequence[Path], *, recursive: bool) -> list[Path]:
    """Expand the given paths into a sorted list of ingestible files.

    Sorted so a batch is reproducible: an unordered walk makes two runs over the same
    directory report failures in different orders, which is miserable to diff.
    """
    discovered: list[Path] = []
    for path in paths:
        if path.is_file():
            discovered.append(path)
        elif path.is_dir():
            pattern = "**/*" if recursive else "*"
            discovered.extend(
                candidate
                for candidate in sorted(path.glob(pattern))
                if candidate.is_file() and candidate.suffix.lower() in _EXTENSION_TO_MEDIA_TYPE
            )
        else:
            raise FileNotFoundError(f"No such file or directory: {path}")
    return sorted(set(discovered))


async def _run_ingest(args: argparse.Namespace, settings: Settings) -> int:
    files = _discover_files(args.paths, recursive=args.recursive)
    if not files:
        print("No ingestible files found.", file=sys.stderr)
        return EXIT_USAGE

    container = ServiceContainer.build(settings)
    await container.startup()

    context = TenantContext.for_service(args.tenant_id, actor_id=args.actor)
    results: list[dict[str, Any]] = []
    failures = 0

    try:
        for path in files:
            media_type = _EXTENSION_TO_MEDIA_TYPE.get(
                path.suffix.lower(), "application/octet-stream"
            )
            try:
                result = await container.pipeline.ingest(
                    IngestionRequest(
                        data=path.read_bytes(),
                        source_system=args.source_system,
                        filename=path.name,
                        declared_media_type=media_type,
                        document_type=args.document_type,
                        patient_id=args.patient_id,
                        deidentification_status=args.deidentification_status,
                        force_reingest=args.force,
                    ),
                    context=context,
                )
                results.append(
                    {
                        "path": str(path),
                        "outcome": "ingested",
                        "document_id": str(result.document_id),
                        "status": str(result.status),
                        "chunk_count": result.chunk_count,
                        "quality_verdict": (
                            str(result.quality.verdict) if result.quality else None
                        ),
                        "quality_score": result.quality.score if result.quality else None,
                    }
                )
            except DuplicateDocumentError as exc:
                # Not a failure: the corpus already contains this content, which is the
                # expected outcome when re-running a batch over a partially-ingested
                # directory. Counting it as an error would make every resumed batch fail.
                results.append(
                    {
                        "path": str(path),
                        "outcome": "duplicate",
                        "document_id": exc.existing_document_id,
                    }
                )
            except CipError as exc:
                failures += 1
                results.append({"path": str(path), "outcome": "failed", "detail": exc.detail})
                if not args.continue_on_error and len(files) == 1:
                    break
    finally:
        await container.shutdown()

    _report(results, as_json=args.json)
    return EXIT_PARTIAL_FAILURE if failures else EXIT_OK


def _report(results: list[dict[str, Any]], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps({"results": results}, indent=2))
        return

    counts = {"ingested": 0, "duplicate": 0, "failed": 0}
    for entry in results:
        counts[entry["outcome"]] = counts.get(entry["outcome"], 0) + 1
        if entry["outcome"] == "ingested":
            print(
                f"  OK        {entry['path']} -> {entry['document_id']} "
                f"({entry['chunk_count']} chunks, {entry['status']}, "
                f"quality={entry['quality_verdict']})"
            )
        elif entry["outcome"] == "duplicate":
            print(f"  DUPLICATE {entry['path']} -> {entry['document_id']}")
        else:
            print(f"  FAILED    {entry['path']}: {entry['detail']}", file=sys.stderr)

    print(
        f"\n{counts['ingested']} ingested, {counts['duplicate']} duplicate, "
        f"{counts['failed']} failed"
    )


async def _run_health(args: argparse.Namespace, settings: Settings) -> int:
    container = ServiceContainer.build(settings)
    await container.startup()

    dependencies: dict[str, Any] = {}
    degraded = False
    try:
        for name, check in (
            ("postgres", container.postgres.health_check),
            ("mongo", container.mongo.health_check),
            ("neo4j", container.neo4j.health_check),
            ("storage", container.storage.health_check),
        ):
            try:
                dependencies[name] = await check()
            except DependencyUnavailableError as exc:
                dependencies[name] = {"status": "unavailable", "detail": exc.detail}
                degraded = True
            except Exception as exc:
                dependencies[name] = {"status": "error", "detail": type(exc).__name__}
                degraded = True
    finally:
        await container.shutdown()

    if args.json:
        print(json.dumps({"degraded": degraded, "dependencies": dependencies}, indent=2))
    else:
        for name, payload in dependencies.items():
            print(f"  {name:10s} {payload.get('status')}")

    return EXIT_DEPENDENCY if degraded else EXIT_OK


def _run_config(args: argparse.Namespace, settings: Settings) -> int:
    description = settings.describe()
    description["pipeline_version"] = PIPELINE_VERSION
    if args.json:
        print(json.dumps(description, indent=2))
    else:
        for key, value in description.items():
            print(f"  {key:24s} {value}")
    return EXIT_OK


def _run_db(args: argparse.Namespace) -> int:
    """Drive Alembic in-process so migrations use the same settings as the service."""
    from alembic import command
    from alembic.config import Config

    root = Path(__file__).resolve().parents[4]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))

    if args.action == "upgrade":
        command.upgrade(config, args.revision)
    elif args.action == "downgrade":
        command.downgrade(config, args.revision)
    else:
        command.current(config, verbose=True)
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code."""
    args = build_parser().parse_args(argv)
    settings = get_settings()
    configure_logging(settings)

    try:
        if args.command == "ingest":
            return asyncio.run(_run_ingest(args, settings))
        if args.command == "health":
            return asyncio.run(_run_health(args, settings))
        if args.command == "config":
            return _run_config(args, settings)
        if args.command == "db":
            return _run_db(args)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except DependencyUnavailableError as exc:
        print(f"dependency unavailable: {exc.detail}", file=sys.stderr)
        return EXIT_DEPENDENCY
    except CipError as exc:
        print(f"error: {exc.detail}", file=sys.stderr)
        return EXIT_USAGE
    except KeyboardInterrupt:  # pragma: no cover - interactive interruption
        print("interrupted", file=sys.stderr)
        return EXIT_USAGE

    return EXIT_USAGE  # pragma: no cover - argparse enforces a known command


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
