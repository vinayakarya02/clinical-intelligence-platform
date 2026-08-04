"""Secret material, behind a provider.

Three implementations: environment variables for development, mounted files for Kubernetes,
and a protocol a KMS or Vault client satisfies. No secret ever appears in a manifest, an
image, or this repository.

The file provider is the one that matters in production. Kubernetes mounts a Secret as files
in a directory, and reading from there rather than from the environment has a concrete
advantage: an environment variable is inherited by every child process and is visible in
``/proc/<pid>/environ``, while a file has permissions.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol, runtime_checkable

from cip_core.errors import CipError

__all__ = [
    "EnvironmentSecrets",
    "FileSecrets",
    "SecretNotFoundError",
    "SecretProvider",
    "StaticSecrets",
]


class SecretNotFoundError(CipError):
    """A required secret is not configured."""

    status = 500
    problem_type = "secret-missing"
    title = "Required secret is not configured"


@runtime_checkable
class SecretProvider(Protocol):
    """Resolves a secret by logical name."""

    def get(self, name: str) -> str | None: ...

    def require(self, name: str) -> str: ...


class _Base:
    """Shared ``require`` semantics.

    The error names the secret but never its value, and never a partial value. A truncated
    secret in an error message is still a leaked prefix.
    """

    def get(self, name: str) -> str | None:  # pragma: no cover - overridden
        raise NotImplementedError

    def require(self, name: str) -> str:
        value = self.get(name)
        if not value:
            raise SecretNotFoundError(f"Secret '{name}' is not configured")
        return value


class EnvironmentSecrets(_Base):
    """Secrets from environment variables. Development and testing only."""

    def __init__(self, *, prefix: str = "CIP_SECRET_", source: dict[str, str] | None = None):
        self._prefix = prefix
        self._source = source if source is not None else dict(os.environ)

    def get(self, name: str) -> str | None:
        return self._source.get(f"{self._prefix}{name.upper()}") or None


class FileSecrets(_Base):
    """Secrets from a mounted directory, one file per secret.

    The Kubernetes shape. Values are read on each access rather than cached, so a rotated
    secret takes effect without a restart — kubelet updates a projected Secret in place, and
    caching would mean rotation silently did nothing until the next deploy.
    """

    def __init__(self, directory: str | Path) -> None:
        self._directory = Path(directory)

    def get(self, name: str) -> str | None:
        # Reject traversal outright rather than sanitising: a secret name is chosen by this
        # codebase, so anything containing a separator is a bug or an attack, not a typo.
        if "/" in name or "\\" in name or ".." in name:
            raise SecretNotFoundError(f"Secret name '{name}' is not a bare name")
        path = self._directory / name
        try:
            return path.read_text(encoding="utf-8").strip() or None
        except (OSError, UnicodeDecodeError):
            return None


class StaticSecrets(_Base):
    """An in-memory map. Tests only; refuse to construct it with anything real."""

    def __init__(self, values: dict[str, str]) -> None:
        self._values = dict(values)

    def get(self, name: str) -> str | None:
        return self._values.get(name) or None
