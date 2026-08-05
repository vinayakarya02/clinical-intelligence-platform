"""Reading secrets from a mounted directory.

Kubernetes mounts a Secret as a directory of files, one per key, and that is the right shape:
files can be rotated in place without a restart, they do not appear in ``/proc/<pid>/environ``,
they are not inherited by child processes, and they do not leak into a crash dump the way an
environment block does. ``deploy/k8s/10-api-deployment.yaml`` mounts exactly that at
``/var/run/secrets/cip`` and announces it with ``CIP_SECRETS_DIR``.

Nothing read it. The deployment layer and the configuration layer were designed independently,
each correct on its own terms, and the two never met: every secret-derived setting — the
database password, the Redis URL, the broker URL, the JWT key — arrived at the process as a
file on disk and was looked for in the environment. In production the settings therefore held
their defaults, and the first thing that needed a real one failed.

This module is the join. It maps each file in the directory to the environment variable the
settings classes already look for, so neither settings system needs to learn about files.

**Never overwrites.** An explicitly-set variable wins over a mounted file, because that is what
lets an operator override one value in a debugging session without unmounting the Secret.
"""

from __future__ import annotations

import os
import pathlib
from dataclasses import dataclass

from cip_core.logging import get_logger

__all__ = [
    "SECRET_FILE_MAP",
    "SecretLoadReport",
    "load_mounted_secrets",
]

_log = get_logger(__name__)

#: Filename in the mounted Secret -> environment variable the settings classes read.
#:
#: Keys match ``deploy/k8s/02-secrets.yaml`` exactly, and every value is a variable one of the
#: settings models actually accepts. The manifest originally declared ``postgres-dsn``,
#: ``api-key-pepper``, and ``jwt-public-key``, none of which correspond to any field — the
#: models take a password rather than a DSN, and in OIDC mode the signing key is fetched from
#: the JWKS endpoint rather than mounted. Setting them would have been rejected outright by
#: ``extra_forbidden``, so the manifest was reconciled to what the process consumes.
#:
#: A file present in the mount but absent here is reported rather than ignored: it means a
#: secret was added to the manifest that nothing reads, which is either dead configuration or a
#: setting silently running on its default.
SECRET_FILE_MAP: dict[str, str] = {
    "postgres-password": "CIP_POSTGRES__PASSWORD",
    "mongo-uri": "CIP_MONGO__URI",
    "neo4j-password": "CIP_NEO4J__PASSWORD",
    "redis-url": "CIP_REDIS_URL",
    "broker-url": "CIP_BROKER_URL",
    "jwt-secret": "CIP_AUTH__JWT_SECRET",
    "analytics-salt": "CIP_ANALYTICS_SALT",
}


@dataclass(frozen=True, slots=True)
class SecretLoadReport:
    """What was found, what was applied, and what was ignored."""

    directory: str = ""
    applied: tuple[str, ...] = ()
    already_set: tuple[str, ...] = ()
    empty: tuple[str, ...] = ()
    unmapped: tuple[str, ...] = ()

    @property
    def present(self) -> bool:
        return bool(self.directory)

    def render(self) -> str:
        if not self.present:
            return "secrets: no CIP_SECRETS_DIR mounted; using environment only"
        parts = [f"secrets from {self.directory}: {len(self.applied)} applied"]
        if self.already_set:
            parts.append(f"{len(self.already_set)} already set in the environment (kept)")
        if self.empty:
            parts.append(f"empty: {', '.join(self.empty)}")
        if self.unmapped:
            parts.append(f"unmapped (nothing reads these): {', '.join(self.unmapped)}")
        return " — ".join(parts)


def load_mounted_secrets(
    directory: str | pathlib.Path | None = None,
    *,
    environ: dict[str, str] | None = None,
) -> SecretLoadReport:
    """Copy mounted secret files into the environment the settings classes read.

    Call this **before** constructing any settings — the composition root does, in its very
    first factory. Returns a report rather than logging and forgetting, so startup validation
    can assert that the secrets a deployed environment requires actually arrived.

    Values are stripped of a single trailing newline only: a passphrase may legitimately end in
    a space, and ``echo -n`` is easy to forget when writing a Secret by hand.
    """
    target = environ if environ is not None else os.environ
    raw = directory if directory is not None else target.get("CIP_SECRETS_DIR", "")
    if not raw:
        return SecretLoadReport()

    path = pathlib.Path(raw)
    if not path.is_dir():
        _log.warning("secrets.directory_missing", directory=str(path))
        return SecretLoadReport(directory=str(path))

    applied: list[str] = []
    already_set: list[str] = []
    empty: list[str] = []
    unmapped: list[str] = []

    for entry in sorted(path.iterdir()):
        # A projected Secret contains ..data and ..2024_01_01 symlink directories; skip them.
        if not entry.is_file() or entry.name.startswith(".."):
            continue

        variable = SECRET_FILE_MAP.get(entry.name)
        if variable is None:
            unmapped.append(entry.name)
            continue

        try:
            value = entry.read_text(encoding="utf-8")
        except OSError as exc:
            _log.warning("secrets.unreadable", file=entry.name, error=str(exc))
            continue

        value = value.removesuffix("\n")
        if not value:
            empty.append(entry.name)
            continue
        if target.get(variable):
            already_set.append(variable)
            continue

        target[variable] = value
        applied.append(variable)

    report = SecretLoadReport(
        directory=str(path),
        applied=tuple(applied),
        already_set=tuple(already_set),
        empty=tuple(empty),
        unmapped=tuple(unmapped),
    )
    # Names only. Logging a secret's value is the one thing this module must never do, and the
    # report is built from names for the same reason.
    _log.info(
        "secrets.loaded",
        directory=str(path),
        applied=len(applied),
        already_set=len(already_set),
        empty=len(empty),
        unmapped=len(unmapped),
    )
    return report
