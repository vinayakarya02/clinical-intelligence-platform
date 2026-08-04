#!/usr/bin/env python
"""Policy checks for Kubernetes manifests.

Schema validation (kubeval) proves a manifest is *well-formed*. It says nothing about whether
it is *safe*: a privileged container running as root with no resource limits and a `latest`
tag is perfectly valid YAML that passes every schema check.

These are the rules a schema cannot express. Each exists because the failure it prevents is
one that only surfaces in production:

- a `latest` tag means the image you scanned and the image running can differ silently
- a missing memory limit means one pod can OOM everything else on its node
- a root container defeats the restricted Pod Security Standard the namespace declares
- a missing probe means Kubernetes cannot tell a wedged pod from a busy one
- a mounted service-account token is a credential nothing here uses and an attacker can

Run directly (``python scripts/validate_manifests.py``) or via the CI ``manifests`` job.
Exits non-zero with one line per violation.
"""

from __future__ import annotations

import pathlib
import sys
from typing import Any, NamedTuple

import yaml

__all__ = ["Violation", "validate_directory", "validate_document"]

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
MANIFEST_DIR = REPO_ROOT / "deploy" / "k8s"

#: Kinds that carry a pod template and are therefore subject to the workload rules.
_WORKLOAD_KINDS = frozenset({"Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob"})

#: Kinds exempt from the probe requirement. A Job runs to completion; a probe would restart it.
_PROBE_EXEMPT = frozenset({"Job", "CronJob"})


class Violation(NamedTuple):
    """One policy failure."""

    path: str
    kind: str
    name: str
    rule: str
    detail: str

    def render(self) -> str:
        return f"{self.path}: {self.kind}/{self.name}: [{self.rule}] {self.detail}"


def _pod_spec(document: dict[str, Any]) -> dict[str, Any] | None:
    """The pod spec inside a workload, whatever the nesting."""
    spec = document.get("spec", {})
    if document.get("kind") == "CronJob":
        return spec.get("jobTemplate", {}).get("spec", {}).get("template", {}).get("spec")
    return spec.get("template", {}).get("spec")


def validate_document(document: dict[str, Any], *, path: str) -> list[Violation]:
    """Check one manifest object."""
    kind = str(document.get("kind", "?"))
    name = str(document.get("metadata", {}).get("name", "?"))
    violations: list[Violation] = []

    def fail(rule: str, detail: str) -> None:
        violations.append(Violation(path=path, kind=kind, name=name, rule=rule, detail=detail))

    if kind == "Namespace":
        labels = document.get("metadata", {}).get("labels", {})
        if labels.get("pod-security.kubernetes.io/enforce") != "restricted":
            fail(
                "pss-restricted",
                "namespace must enforce the restricted Pod Security Standard",
            )
        return violations

    if kind not in _WORKLOAD_KINDS:
        return violations

    pod = _pod_spec(document)
    if pod is None:
        fail("pod-spec", "workload has no pod template")
        return violations

    pod_security = pod.get("securityContext", {})
    if not pod_security.get("runAsNonRoot"):
        fail("run-as-non-root", "pod securityContext must set runAsNonRoot: true")
    if pod_security.get("seccompProfile", {}).get("type") not in ("RuntimeDefault", "Localhost"):
        fail("seccomp", "pod securityContext must set a seccompProfile")

    # A token nothing uses is a credential an attacker can. Every workload here talks to
    # datastores, never to the Kubernetes API.
    if pod.get("automountServiceAccountToken") is not False:
        fail(
            "no-sa-token",
            "automountServiceAccountToken must be false; nothing here calls the Kubernetes API",
        )

    containers = list(pod.get("containers", []))
    if not containers:
        fail("containers", "workload declares no containers")

    for container in containers:
        cname = container.get("name", "?")
        image = str(container.get("image", ""))

        if not image:
            fail("image", f"container '{cname}' has no image")
        elif image.endswith(":latest") or ":" not in image.rsplit("/", 1)[-1]:
            fail(
                "image-tag",
                f"container '{cname}' uses a floating tag ({image!r}); the image scanned and "
                "the image running can differ",
            )

        security = container.get("securityContext", {})
        if security.get("allowPrivilegeEscalation") is not False:
            fail(
                "no-privilege-escalation",
                f"container '{cname}' must set allowPrivilegeEscalation: false",
            )
        if security.get("readOnlyRootFilesystem") is not True:
            fail("read-only-rootfs", f"container '{cname}' must set readOnlyRootFilesystem: true")
        if security.get("capabilities", {}).get("drop") != ["ALL"]:
            fail("drop-capabilities", f"container '{cname}' must drop ALL capabilities")
        if security.get("privileged"):
            fail("no-privileged", f"container '{cname}' must not be privileged")

        resources = container.get("resources", {})
        requests = resources.get("requests", {})
        limits = resources.get("limits", {})
        if not requests.get("cpu") or not requests.get("memory"):
            fail("resource-requests", f"container '{cname}' must request cpu and memory")
        if not limits.get("memory"):
            # Memory only. A CPU limit throttles rather than evicts, and throttling a
            # latency-sensitive service produces p99 spikes that read as an application bug —
            # so its absence is deliberate and not a violation.
            fail("memory-limit", f"container '{cname}' must set a memory limit")

        if kind not in _PROBE_EXEMPT and not container.get("livenessProbe"):
            fail("liveness-probe", f"container '{cname}' has no livenessProbe")

    return violations


def validate_directory(directory: pathlib.Path = MANIFEST_DIR) -> list[Violation]:
    """Check every manifest in ``directory``."""
    violations: list[Violation] = []
    for path in sorted(directory.glob("*.yaml")):
        text = path.read_text(encoding="utf-8")
        for document in yaml.safe_load_all(text):
            if not document:
                continue
            violations.extend(validate_document(document, path=str(path.relative_to(REPO_ROOT))))
    return violations


def main() -> int:
    if not MANIFEST_DIR.is_dir():
        print(f"No manifest directory at {MANIFEST_DIR}", file=sys.stderr)
        return 1

    violations = validate_directory()
    for violation in violations:
        print(violation.render(), file=sys.stderr)

    if violations:
        print(f"\n{len(violations)} policy violation(s)", file=sys.stderr)
        return 1

    print("All manifests satisfy the deployment policy.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
