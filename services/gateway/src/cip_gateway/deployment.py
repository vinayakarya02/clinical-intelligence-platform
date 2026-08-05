"""Static validation of the deployment assets.

Docker, a cluster, and a registry are not available in every environment a change is made in,
and "it built on my machine" is not a property anyone can rely on. What *can* be checked
everywhere is whether the assets say what they must say — and most deployment incidents are
exactly that kind of error rather than a build failure.

The check that earns the module is **package drift**: the image copies an explicit list of
source trees, and a phase that adds a service without adding it to that list produces an image
that builds, starts, passes its health check, and cannot import half the platform. Nothing in a
Dockerfile linter catches it, because the Dockerfile is valid.

This validates; it does not build, push, or apply. Anything requiring a daemon or a cluster is
reported as **unverified** rather than assumed to pass — a validator that reports success for a
check it did not run is worse than one that does not run it.
"""

from __future__ import annotations

import pathlib
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import yaml

from cip_core.logging import get_logger

__all__ = [
    "DeploymentReport",
    "Finding",
    "Severity",
    "validate_deployment",
]

_log = get_logger(__name__)


class Severity(StrEnum):
    """How much a finding matters."""

    BLOCKER = "blocker"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNVERIFIED = "unverified"
    """A check that could not be run here — a build, a cluster apply, a registry pull. Reported
    so the gap is visible rather than mistaken for a pass."""

    @property
    def fails_the_gate(self) -> bool:
        return self in (Severity.BLOCKER, Severity.HIGH)


@dataclass(frozen=True, slots=True)
class Finding:
    """One thing wrong, or one thing not checked."""

    severity: Severity
    asset: str
    detail: str
    remedy: str = ""

    def render(self) -> str:
        remedy = f" — {self.remedy}" if self.remedy else ""
        return f"[{self.severity.value:<10}] {self.asset}: {self.detail}{remedy}"

    def to_json(self) -> dict[str, Any]:
        return {
            "severity": str(self.severity),
            "asset": self.asset,
            "detail": self.detail,
            "remedy": self.remedy,
        }


@dataclass(frozen=True, slots=True)
class DeploymentReport:
    """Everything static validation concluded."""

    findings: tuple[Finding, ...] = ()
    assets_checked: tuple[str, ...] = ()
    checks_run: int = 0

    @property
    def blocking(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity.fails_the_gate)

    @property
    def unverified(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity is Severity.UNVERIFIED)

    @property
    def ok(self) -> bool:
        return not self.blocking

    def to_json(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checksRun": self.checks_run,
            "assets": list(self.assets_checked),
            "findings": [f.to_json() for f in self.findings],
            "blocking": len(self.blocking),
            "unverified": len(self.unverified),
        }

    def render(self) -> str:
        lines = [
            f"deployment validation {'ok' if self.ok else 'FAILED'} — "
            f"{self.checks_run} check(s) over {len(self.assets_checked)} asset(s)"
        ]
        lines.extend(f"  {finding.render()}" for finding in self.findings)
        if not self.findings:
            lines.append("  no findings")
        return "\n".join(lines)


#: Source trees that must be present in the runtime image. Derived from the repository rather
#: than hardcoded, so a new service is covered the day it is added.
def _expected_packages(root: pathlib.Path) -> set[str]:
    found: set[str] = set()
    for parent in ("libs", "services"):
        base = root / parent
        if not base.is_dir():
            continue
        for entry in sorted(base.iterdir()):
            if (entry / "src").is_dir():
                found.add(f"{parent}/{entry.name}/src")
    return found


def _validate_dockerfile(path: pathlib.Path, root: pathlib.Path) -> tuple[list[Finding], int]:
    findings: list[Finding] = []
    text = path.read_text(encoding="utf-8")
    checks = 0

    checks += 1
    if "AS builder" not in text or text.count("FROM ") < 2:
        findings.append(
            Finding(
                Severity.MEDIUM,
                "docker/Dockerfile",
                "not a multi-stage build",
                "build dependencies end up in the runtime image",
            )
        )

    checks += 1
    if not re.search(r"^USER\s+(?!root)\S+", text, re.M):
        findings.append(
            Finding(
                Severity.BLOCKER,
                "docker/Dockerfile",
                "no non-root USER directive",
                "a container running as root defeats the restricted Pod Security Standard",
            )
        )

    checks += 1
    if "HEALTHCHECK" not in text:
        findings.append(
            Finding(
                Severity.MEDIUM,
                "docker/Dockerfile",
                "no HEALTHCHECK",
                "compose and swarm have no way to know the process is serving",
            )
        )

    checks += 1
    for match in re.finditer(r"^FROM\s+(\S+)", text, re.M):
        image = match.group(1)
        if image.endswith(":latest") or (":" not in image and "$" not in image):
            findings.append(
                Finding(
                    Severity.HIGH,
                    "docker/Dockerfile",
                    f"base image {image!r} is unpinned",
                    "an unpinned base makes the image unreproducible and silently changes",
                )
            )

    # The check that earns the module.
    checks += 1
    expected = _expected_packages(root)
    copied = {
        match.group(1).strip()
        for match in re.finditer(r"^COPY\s+(?:--\S+\s+)*(\S+/src)\s", text, re.M)
    }
    missing = sorted(expected - copied)
    if missing:
        findings.append(
            Finding(
                Severity.BLOCKER,
                "docker/Dockerfile",
                f"source trees present in the repository but not copied into the image: "
                f"{', '.join(missing)}",
                "the image builds, starts, passes its health check, and cannot import them",
            )
        )
    return findings, checks


def _validate_compose(path: pathlib.Path) -> tuple[list[Finding], int]:
    findings: list[Finding] = []
    checks = 0
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        return [Finding(Severity.BLOCKER, "docker/docker-compose.yml", f"invalid YAML: {exc}")], 1

    services = document.get("services") or {}
    checks += 1
    if not services:
        findings.append(
            Finding(Severity.BLOCKER, "docker/docker-compose.yml", "declares no services")
        )

    for name, service in sorted(services.items()):
        if not isinstance(service, dict):
            continue
        image = str(service.get("image", ""))
        checks += 1
        if image.endswith(":latest"):
            findings.append(
                Finding(
                    Severity.MEDIUM,
                    f"docker-compose:{name}",
                    f"image {image!r} uses the latest tag",
                    "a development stack that changes underneath you is not reproducible",
                )
            )
        checks += 1
        if "healthcheck" not in service and "build" not in service:
            findings.append(
                Finding(
                    Severity.MEDIUM,
                    f"docker-compose:{name}",
                    "no healthcheck",
                    "dependents start before it is ready and fail their first call",
                )
            )
    return findings, checks


def _validate_k8s(directory: pathlib.Path) -> tuple[list[Finding], int]:
    findings: list[Finding] = []
    checks = 0
    manifests: list[tuple[str, dict[str, Any]]] = []

    for path in sorted(directory.glob("*.yaml")):
        try:
            for document in yaml.safe_load_all(path.read_text(encoding="utf-8")):
                if isinstance(document, dict) and document.get("kind"):
                    manifests.append((path.name, document))
        except yaml.YAMLError as exc:
            findings.append(Finding(Severity.BLOCKER, f"k8s/{path.name}", f"invalid YAML: {exc}"))

    kinds = {document.get("kind") for _, document in manifests}

    checks += 1
    if "NetworkPolicy" not in kinds:
        findings.append(
            Finding(
                Severity.HIGH,
                "k8s",
                "no NetworkPolicy",
                "without one every pod can reach every other pod",
            )
        )

    for filename, document in manifests:
        kind = document.get("kind")
        if kind not in ("Deployment", "StatefulSet"):
            continue
        spec = (document.get("spec") or {}).get("template", {}).get("spec", {})
        pod_security = spec.get("securityContext") or {}
        containers = spec.get("containers") or []

        checks += 1
        if not pod_security.get("runAsNonRoot"):
            findings.append(
                Finding(
                    Severity.BLOCKER,
                    f"k8s/{filename}",
                    f"{kind} does not set runAsNonRoot",
                    "the restricted Pod Security Standard rejects it",
                )
            )

        for container in containers:
            name = container.get("name", "?")
            security = container.get("securityContext") or {}
            resources = container.get("resources") or {}
            image = str(container.get("image", ""))

            checks += 1
            if security.get("allowPrivilegeEscalation") is not False:
                findings.append(
                    Finding(
                        Severity.HIGH,
                        f"k8s/{filename}:{name}",
                        "allowPrivilegeEscalation is not explicitly false",
                    )
                )
            checks += 1
            if not security.get("readOnlyRootFilesystem"):
                findings.append(
                    Finding(
                        Severity.MEDIUM,
                        f"k8s/{filename}:{name}",
                        "root filesystem is writable",
                    )
                )
            checks += 1
            if not resources.get("requests") or not resources.get("limits"):
                findings.append(
                    Finding(
                        Severity.HIGH,
                        f"k8s/{filename}:{name}",
                        "missing resource requests or limits",
                        "a container without limits can starve every other pod on the node",
                    )
                )
            checks += 1
            if image.endswith(":latest") or ":" not in image:
                findings.append(
                    Finding(
                        Severity.HIGH,
                        f"k8s/{filename}:{name}",
                        f"image {image!r} is unpinned",
                        "a rollout cannot be reproduced or rolled back",
                    )
                )
            # Probes are only meaningful on the serving deployment; a worker has no port.
            if "api" in filename:
                for probe in ("startupProbe", "livenessProbe", "readinessProbe"):
                    checks += 1
                    if probe not in container:
                        findings.append(
                            Finding(
                                Severity.HIGH,
                                f"k8s/{filename}:{name}",
                                f"no {probe}",
                                "Kubernetes asks three different questions and one probe "
                                "answers at most one of them correctly",
                            )
                        )

    for filename, document in manifests:
        if document.get("kind") != "Secret":
            continue
        checks += 1
        data = document.get("data") or document.get("stringData") or {}
        real = {
            key: value
            for key, value in data.items()
            if value and "CHANGE" not in str(value).upper() and "REPLACE" not in str(value).upper()
        }
        if real:
            findings.append(
                Finding(
                    Severity.BLOCKER,
                    f"k8s/{filename}",
                    f"Secret carries inline values for {sorted(real)}",
                    "committed secrets are compromised secrets; use a sealed secret or an "
                    "external store",
                )
            )
    return findings, checks


def _validate_ci(path: pathlib.Path) -> tuple[list[Finding], int]:
    findings: list[Finding] = []
    checks = 0
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        return [Finding(Severity.BLOCKER, ".github/workflows/ci.yml", f"invalid YAML: {exc}")], 1

    body = path.read_text(encoding="utf-8")
    for gate, severity in (
        ("ruff format", Severity.MEDIUM),
        ("ruff check", Severity.HIGH),
        ("pyright", Severity.HIGH),
        ("pytest", Severity.BLOCKER),
    ):
        checks += 1
        if gate not in body:
            findings.append(
                Finding(
                    severity,
                    ".github/workflows/ci.yml",
                    f"no {gate!r} step",
                    "a gate that does not run in CI is a gate that is not enforced",
                )
            )

    checks += 1
    jobs = document.get("jobs") or {}
    if not jobs:
        findings.append(Finding(Severity.BLOCKER, ".github/workflows/ci.yml", "declares no jobs"))
    return findings, checks


def validate_deployment(root: pathlib.Path | str | None = None) -> DeploymentReport:
    """Validate every deployment asset that exists.

    A missing asset is a finding, not a silent pass — a repository with no Kubernetes manifests
    and a validator that reports success has told the operator nothing.
    """
    base = pathlib.Path(root) if root else pathlib.Path(__file__).resolve().parents[4]
    findings: list[Finding] = []
    checked: list[str] = []
    total = 0

    dockerfile = base / "docker/Dockerfile"
    if dockerfile.is_file():
        checked.append("docker/Dockerfile")
        new, count = _validate_dockerfile(dockerfile, base)
        findings.extend(new)
        total += count
    else:
        findings.append(Finding(Severity.HIGH, "docker/Dockerfile", "not present"))

    compose = base / "docker/docker-compose.yml"
    if compose.is_file():
        checked.append("docker/docker-compose.yml")
        new, count = _validate_compose(compose)
        findings.extend(new)
        total += count
    else:
        findings.append(Finding(Severity.MEDIUM, "docker/docker-compose.yml", "not present"))

    k8s = base / "deploy/k8s"
    if k8s.is_dir():
        checked.append("deploy/k8s")
        new, count = _validate_k8s(k8s)
        findings.extend(new)
        total += count
    else:
        findings.append(Finding(Severity.HIGH, "deploy/k8s", "no Kubernetes manifests"))

    ci = base / ".github/workflows/ci.yml"
    if ci.is_file():
        checked.append(".github/workflows/ci.yml")
        new, count = _validate_ci(ci)
        findings.extend(new)
        total += count
    else:
        findings.append(Finding(Severity.HIGH, ".github/workflows/ci.yml", "no CI workflow"))

    # What this cannot check here, stated rather than assumed.
    findings.extend(
        Finding(Severity.UNVERIFIED, asset, detail, remedy)
        for asset, detail, remedy in (
            ("docker build", "not executed", "requires a Docker daemon"),
            ("docker compose up", "not executed", "requires a Docker daemon"),
            ("kubectl apply --dry-run=server", "not executed", "requires a cluster"),
            ("image vulnerability scan", "not executed", "requires a registry and a scanner"),
        )
    )

    report = DeploymentReport(
        findings=tuple(findings), assets_checked=tuple(checked), checks_run=total
    )
    _log.info(
        "deployment.validated",
        ok=report.ok,
        checks=total,
        blocking=len(report.blocking),
        unverified=len(report.unverified),
    )
    return report


@dataclass(slots=True)
class DeploymentGate:
    """A pass/fail gate over the report, for CI.

    Separate from the report so a caller can decide what blocks: a pre-merge gate fails on
    Blocker and High, and a nightly audit reports Medium and Low without failing anything.
    """

    fail_on: frozenset[Severity] = field(
        default_factory=lambda: frozenset({Severity.BLOCKER, Severity.HIGH})
    )

    def evaluate(self, report: DeploymentReport) -> tuple[bool, tuple[Finding, ...]]:
        blocking = tuple(f for f in report.findings if f.severity in self.fail_on)
        return (not blocking, blocking)
