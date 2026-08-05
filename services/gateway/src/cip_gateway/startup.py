"""Startup validation: everything checked before the first request, not after it.

A platform that starts successfully and fails on the first real request has told the operator
nothing useful. The rollout completes, the pods report Ready, traffic shifts, and the failure
arrives as a spike of 500s attributable to nothing. Every check here exists to move a class of
failure from *first request in production* to *process start*, where a bad deployment fails its
readiness gate and the previous version keeps serving.

Four independent checks, in the order their failures are cheapest to diagnose:

1. **Configuration** — is what we were told coherent, and safe for this environment?
2. **Dependency graph** — do the services agree about who needs whom?
3. **Routes** — does the declared HTTP surface agree with the services that exist?
4. **Wiring** — do the services actually construct?

The environment decides how strict this is. Development tolerates a default salt and an
in-memory queue because the alternative is that nobody can run anything locally. Production
tolerates neither, and the distinction is enforced here rather than left to a deployment
checklist that is correct until the day it is not.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from cip_core.logging import get_logger
from cip_gateway.container import ServiceContainer
from cip_gateway.routes import RouteRegistry, platform_routes

__all__ = [
    "CheckStatus",
    "StartupCheck",
    "StartupError",
    "StartupValidation",
    "validate_startup",
]

_log = get_logger(__name__)

#: Values that mean "nobody set this". Matched case-insensitively as substrings, because the
#: realistic failure is a half-edited placeholder, not a pristine one.
_PLACEHOLDERS = ("change-me", "changeme", "replace", "placeholder", "example", "todo", "xxx")

#: Secrets that must carry a real, non-placeholder value in production regardless of anything
#: else. The de-identification salt is the only unconditional one, and it is unconditional
#: because its default is not merely weak but actively harmful: a known salt makes every
#: pseudonym in the warehouse reversible by anyone holding the source identifiers, which turns
#: a de-identified dataset back into PHI.
#:
#: The authentication secret is deliberately *not* here — which one is required depends on the
#: auth mode, and a check that demands a fixed variable name is a check that passes in the one
#: configuration nobody deploys. See :func:`_check_auth_secret`.
_PRODUCTION_SECRETS = (
    ("CIP_ANALYTICS_SALT", "de-identification salt — a known salt makes pseudonyms reversible"),
)


class CheckStatus(StrEnum):
    PASSED = "passed"
    WARNING = "warning"
    FAILED = "failed"

    @property
    def is_fatal(self) -> bool:
        return self is CheckStatus.FAILED


@dataclass(frozen=True, slots=True)
class StartupCheck:
    name: str
    status: CheckStatus
    detail: str
    remedy: str = ""

    def render(self) -> str:
        remedy = f"\n      remedy: {self.remedy}" if self.remedy else ""
        return f"[{self.status.value:<7}] {self.name}: {self.detail}{remedy}"

    def to_json(self) -> dict[str, str]:
        return {
            "name": self.name,
            "status": str(self.status),
            "detail": self.detail,
            "remedy": self.remedy,
        }


class StartupError(RuntimeError):
    """Raised when validation fails and the process must not serve.

    Carries the whole validation rather than only the first failure: an operator reading a crash
    loop wants every problem at once, not one per restart.
    """

    def __init__(self, validation: StartupValidation) -> None:
        self.validation = validation
        failures = "; ".join(check.detail for check in validation.failures)
        super().__init__(f"startup validation failed ({len(validation.failures)}): {failures}")


@dataclass(frozen=True, slots=True)
class StartupValidation:
    checks: tuple[StartupCheck, ...] = ()
    environment: str = "unknown"
    duration_ms: float = 0.0
    container: ServiceContainer | None = field(default=None, compare=False)
    registry: RouteRegistry | None = field(default=None, compare=False)

    @property
    def failures(self) -> tuple[StartupCheck, ...]:
        return tuple(check for check in self.checks if check.status.is_fatal)

    @property
    def warnings(self) -> tuple[StartupCheck, ...]:
        return tuple(check for check in self.checks if check.status is CheckStatus.WARNING)

    @property
    def ok(self) -> bool:
        return not self.failures

    def raise_for_status(self) -> StartupValidation:
        if not self.ok:
            raise StartupError(self)
        return self

    def to_json(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "environment": self.environment,
            "durationMs": round(self.duration_ms, 2),
            "checks": [check.to_json() for check in self.checks],
            "failures": len(self.failures),
            "warnings": len(self.warnings),
        }

    def render(self) -> str:
        head = (
            f"startup validation {'ok' if self.ok else 'FAILED'} "
            f"[{self.environment}] in {self.duration_ms:.0f} ms — "
            f"{len(self.checks)} check(s), {len(self.failures)} failure(s), "
            f"{len(self.warnings)} warning(s)"
        )
        return "\n".join([head, *(f"  {check.render()}" for check in self.checks)])


def _looks_unset(value: str) -> bool:
    lowered = value.strip().lower()
    return not lowered or any(token in lowered for token in _PLACEHOLDERS)


def _check_configuration(container: ServiceContainer, production: bool) -> list[StartupCheck]:
    checks: list[StartupCheck] = []
    try:
        settings = container.get("settings")
    except Exception as exc:
        return [
            StartupCheck(
                "config.load",
                CheckStatus.FAILED,
                f"settings could not be loaded: {exc}",
                "the process cannot decide anything else until this is fixed",
            )
        ]

    checks.append(StartupCheck("config.load", CheckStatus.PASSED, "settings loaded and validated"))
    platform = settings["platform"]

    for name, purpose in _PRODUCTION_SECRETS:
        value = os.environ.get(name, "")
        if not _looks_unset(value):
            checks.append(StartupCheck(f"config.secret.{name}", CheckStatus.PASSED, "set"))
        elif production:
            checks.append(
                StartupCheck(
                    f"config.secret.{name}",
                    CheckStatus.FAILED,
                    f"{name} is unset or still a placeholder in production ({purpose})",
                    f"set {name} from the secret store before the pod starts",
                )
            )
        else:
            checks.append(
                StartupCheck(
                    f"config.secret.{name}",
                    CheckStatus.WARNING,
                    f"{name} unset — a development default is in use ({purpose})",
                    "harmless here; fatal in production",
                )
            )

    checks.append(_check_auth_secret(settings["core"], production))

    telemetry = platform.telemetry
    if production and telemetry.record_prompt_content:
        checks.append(
            StartupCheck(
                "config.telemetry.prompts",
                CheckStatus.FAILED,
                "prompt content recording is enabled in production",
                "prompts carry PHI; recording them writes PHI into the trace backend",
            )
        )
    else:
        checks.append(
            StartupCheck(
                "config.telemetry.prompts", CheckStatus.PASSED, "prompt content not recorded"
            )
        )

    if production and platform.cache.backend == "memory":
        checks.append(
            StartupCheck(
                "config.cache",
                CheckStatus.WARNING,
                "in-memory cache in production — each replica has its own, and hit rates fall "
                "with every scale-out",
                "point CachePolicy.redis_url at the shared cache",
            )
        )
    else:
        checks.append(
            StartupCheck(
                "config.cache", CheckStatus.PASSED, f"cache backend {platform.cache.backend!r}"
            )
        )

    # The rate limiter is a token bucket held in this process. With N replicas behind a Service
    # the effective per-tenant limit is N times the configured one — a limit that loosens every
    # time the deployment scales, which is exactly when it is most needed. Reported rather than
    # silently accepted, because "600 requests per minute" in a runbook and 600 x N in production
    # is the kind of gap nobody finds until a tenant saturates the cluster.
    limits = platform.limits
    if production and platform.cache.backend == "memory":
        checks.append(
            StartupCheck(
                "config.rate_limit",
                CheckStatus.WARNING,
                f"rate limiting is per replica: the configured "
                f"{limits.requests_per_minute_per_tenant}/min per tenant is enforced "
                f"independently by each pod",
                "back the limiter with the shared cache so the limit is cluster-wide",
            )
        )
    else:
        checks.append(
            StartupCheck(
                "config.rate_limit",
                CheckStatus.PASSED,
                f"{limits.requests_per_minute_per_tenant}/min per tenant, "
                f"{limits.requests_per_minute_per_principal}/min per principal",
            )
        )

    if production and platform.queue.backend == "memory":
        checks.append(
            StartupCheck(
                "config.queue",
                CheckStatus.FAILED,
                "in-memory queue in production — every queued job is lost when a pod restarts, "
                "silently",
                "configure QueuePolicy.broker_url",
            )
        )
    else:
        checks.append(
            StartupCheck(
                "config.queue", CheckStatus.PASSED, f"queue backend {platform.queue.backend!r}"
            )
        )
    return checks


def _check_auth_secret(core: Any, production: bool) -> StartupCheck:
    """The credential the *configured* auth mode actually needs.

    Which secret must be present is a function of the mode, not a fixed variable name. Under
    OIDC — the deployed default — verification keys are fetched from the tenant IdP's JWKS
    endpoint and no signing key is mounted at all, so demanding one would fail every real
    deployment. Under HS256 the process signs its own tokens and the secret is mandatory.
    """
    auth = core.auth
    if not auth.enabled:
        return StartupCheck(
            "config.auth",
            CheckStatus.FAILED if production else CheckStatus.WARNING,
            "authentication is disabled",
            "every request is unauthenticated; only ever acceptable locally",
        )

    if str(auth.mode) == "oidc":
        if auth.jwks_url:
            return StartupCheck("config.auth", CheckStatus.PASSED, f"OIDC against {auth.jwks_url}")
        return StartupCheck(
            "config.auth",
            CheckStatus.FAILED,
            "auth mode is 'oidc' but CIP_AUTH__JWKS_URL is unset",
            "no key source means no token can be verified",
        )

    secret = (
        auth.jwt_secret.get_secret_value() if hasattr(auth.jwt_secret, "get_secret_value") else ""
    )
    if production:
        return StartupCheck(
            "config.auth",
            CheckStatus.FAILED,
            "auth mode 'local_hs256' is a development shortcut; deployed environments federate "
            "with the tenant IdP",
            "set CIP_AUTH__MODE=oidc",
        )
    if _looks_unset(secret):
        return StartupCheck(
            "config.auth",
            CheckStatus.WARNING,
            "HS256 with no signing secret — an ephemeral key is generated per process",
            "tokens do not survive a restart, and replicas do not agree",
        )
    return StartupCheck("config.auth", CheckStatus.PASSED, "HS256 with a configured secret")


def _check_dependencies(container: ServiceContainer) -> list[StartupCheck]:
    try:
        order = container.build_order()
    except Exception as exc:
        return [
            StartupCheck(
                "dependencies.graph",
                CheckStatus.FAILED,
                str(exc),
                "the container cannot decide a start order",
            )
        ]
    return [
        StartupCheck(
            "dependencies.graph",
            CheckStatus.PASSED,
            f"{len(order)} service(s) resolve to an acyclic order: {' -> '.join(order)}",
        )
    ]


def _check_routes(container: ServiceContainer, registry: RouteRegistry) -> list[StartupCheck]:
    issues = registry.validate(container)
    if not issues:
        return [
            StartupCheck(
                "routes.registry",
                CheckStatus.PASSED,
                f"{len(registry.routes)} route(s) over {len(registry.services())} service(s), "
                f"no duplicates, no dead routes, no unreachable services",
            )
        ]
    return [
        StartupCheck("routes.registry", CheckStatus.FAILED, issue.detail, issue.kind.value)
        for issue in issues
    ]


def _check_wiring(container: ServiceContainer) -> list[StartupCheck]:
    from cip_gateway.container import ServiceState

    report = container.start()
    failed = tuple(s for s in report.started if s.state is ServiceState.FAILED)
    healthy = tuple(s for s in report.started if s.state is ServiceState.STARTED)

    checks = [
        StartupCheck(
            "wiring.startup",
            CheckStatus.PASSED if report.ok else CheckStatus.FAILED,
            f"{len(healthy)} started, {len(report.degraded)} degraded, {len(failed)} failed"
            + (f"; aborted on {report.aborted_on!r}" if report.aborted_on else ""),
        )
    ]
    checks.extend(
        StartupCheck(
            f"wiring.{status.name}",
            CheckStatus.WARNING,
            f"non-critical service {status.name!r} is degraded: {status.error}",
            "the platform serves without it; the feature it backs is unavailable",
        )
        for status in report.degraded
    )
    checks.extend(
        StartupCheck(
            f"wiring.{status.name}",
            CheckStatus.FAILED,
            f"critical service {status.name!r} failed to start: {status.error}",
        )
        for status in failed
    )
    return checks


def validate_startup(
    container: ServiceContainer,
    *,
    registry: RouteRegistry | None = None,
    production: bool | None = None,
    start_services: bool = True,
) -> StartupValidation:
    """Run every startup check and report all of them.

    Deliberately does **not** stop at the first failure. A caller restarting on each successive
    error learns one problem per crash loop; a caller handed the whole list fixes them together.
    """
    began = time.perf_counter()
    registry = registry if registry is not None else platform_routes()

    checks: list[StartupCheck] = []
    checks.extend(_check_dependencies(container))

    environment = "unknown"
    if production is None:
        try:
            environment = str(container.get("settings")["platform"].environment)
        except Exception:
            environment = "unknown"
        production = environment == "production"
    else:
        environment = "production" if production else "development"

    checks.extend(_check_configuration(container, production))
    checks.extend(_check_routes(container, registry))
    if start_services:
        checks.extend(_check_wiring(container))

    validation = StartupValidation(
        checks=tuple(checks),
        environment=environment,
        duration_ms=(time.perf_counter() - began) * 1000,
        container=container,
        registry=registry,
    )
    _log.info(
        "startup.validated",
        ok=validation.ok,
        environment=environment,
        checks=len(checks),
        failures=len(validation.failures),
        warnings=len(validation.warnings),
    )
    return validation
