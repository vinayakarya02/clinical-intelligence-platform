"""The composition root.

Every phase before this one built its own objects in its own demo. That is correct for a phase
and wrong for a system: nothing decided the order services start in, what happens when one of
them cannot start, or which of them the platform can run without.

This is the one module allowed to know about every service. That is what a composition root is
for, and confining it to one file is what keeps the services themselves free of each other — a
boundary test asserts no service imports another, and this module is the declared exception.

Four properties are load-bearing.

**Lazy factories, not eager imports.** A service is registered as a factory and built when
something needs it. Importing every service at module load couples startup time to the slowest
import and makes a single broken module take down a process that did not need it.

**Topological build order with cycle detection.** A container that cannot detect a dependency
cycle does not fail — it recurses until the stack runs out, and the traceback points at the
container rather than at the cycle.

**Fail fast on critical services, degrade on the rest.** A platform that starts with a broken
knowledge graph and discovers it on the first query has moved a startup error into a user-facing
one. A platform that refuses to start because the analytics warehouse is empty has turned a
reporting gap into an outage. Which is which is declared, not inferred.

**Shutdown in reverse.** Stopping a dependency before its dependents means the dependents fail
on the way down, and the resulting error log is about the wrong component.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from cip_core.errors import CipError
from cip_core.logging import get_logger

__all__ = [
    "ContainerError",
    "ServiceContainer",
    "ServiceSpec",
    "ServiceState",
    "ServiceStatus",
    "StartupReport",
]

_log = get_logger(__name__)


class ContainerError(CipError):
    """A service could not be registered, resolved, or started."""

    status = 500
    problem_type = "container-error"
    title = "Service composition failed"


class ServiceState(StrEnum):
    """Where one service is in its lifecycle."""

    REGISTERED = "registered"
    """Declared but not built. The normal state before startup."""
    STARTED = "started"
    DEGRADED = "degraded"
    """Failed to start, and declared non-critical. The platform runs without it and says so."""
    FAILED = "failed"
    STOPPED = "stopped"

    @property
    def is_usable(self) -> bool:
        return self is ServiceState.STARTED

    @property
    def blocks_readiness(self) -> bool:
        """Whether this state should keep the replica out of the load balancer.

        ``DEGRADED`` does not. A degraded non-critical service is the case readiness exists to
        tolerate — removing every replica because the cache is down converts a slowdown into an
        outage.
        """
        return self is ServiceState.FAILED


@dataclass(frozen=True, slots=True)
class ServiceSpec:
    """How to build one service.

    ``factory`` receives the container so it can resolve its own dependencies. It is given the
    container rather than the resolved objects because a service's dependency list is a fact
    about the service, and threading positional arguments through means changing every call site
    when it changes.
    """

    name: str
    factory: Callable[[ServiceContainer], Any]
    depends_on: tuple[str, ...] = ()
    critical: bool = True
    """A non-critical service that fails to start leaves the platform running and degraded.
    Declared per service, because "can we run without this" is a product decision and not
    something a container can infer."""
    description: str = ""
    stop: Callable[[Any], None] | None = None
    """Called on shutdown, newest first. Absent for services that hold no resource."""

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ContainerError("ServiceSpec.name must not be empty")
        if self.name in self.depends_on:
            raise ContainerError(f"service {self.name!r} declares itself as a dependency")


@dataclass(frozen=True, slots=True)
class ServiceStatus:
    """What happened to one service."""

    name: str
    state: ServiceState
    critical: bool
    build_ms: float = 0.0
    error: str = ""

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            "state": str(self.state),
            "critical": self.critical,
            "buildMs": round(self.build_ms, 3),
        }
        if self.error:
            payload["error"] = self.error
        return payload


@dataclass(frozen=True, slots=True)
class StartupReport:
    """The outcome of bringing the platform up."""

    started: tuple[ServiceStatus, ...] = ()
    order: tuple[str, ...] = ()
    duration_ms: float = 0.0
    aborted_on: str = ""
    """The critical service whose failure stopped startup, if any."""

    @property
    def ok(self) -> bool:
        return not self.aborted_on and not any(s.state is ServiceState.FAILED for s in self.started)

    @property
    def degraded(self) -> tuple[ServiceStatus, ...]:
        return tuple(s for s in self.started if s.state is ServiceState.DEGRADED)

    def to_json(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "order": list(self.order),
            "services": [s.to_json() for s in self.started],
            "degraded": [s.name for s in self.degraded],
            "abortedOn": self.aborted_on,
            "durationMs": round(self.duration_ms, 3),
        }

    def render(self) -> str:
        lines = [f"startup {'ok' if self.ok else 'FAILED'} in {self.duration_ms:.0f} ms"]
        for status in self.started:
            marker = {
                ServiceState.STARTED: " ",
                ServiceState.DEGRADED: "~",
                ServiceState.FAILED: "!",
            }.get(status.state, " ")
            detail = f"  {status.error}" if status.error else ""
            lines.append(
                f"  {marker} {status.name:<28} {status.state.value:<10} "
                f"{status.build_ms:>8.1f} ms{detail}"
            )
        if self.aborted_on:
            lines.append(f"  aborted: critical service {self.aborted_on!r} failed to start")
        return "\n".join(lines)


class ServiceContainer:
    """Builds, holds, and tears down the platform's services."""

    def __init__(self) -> None:
        self._specs: dict[str, ServiceSpec] = {}
        self._instances: dict[str, Any] = {}
        self._status: dict[str, ServiceStatus] = {}
        self._order: tuple[str, ...] = ()
        self._started = False
        self._resolving: list[str] = []

    def register(self, spec: ServiceSpec) -> ServiceContainer:
        """Declare a service. Returns self so registration reads as a list."""
        if self._started:
            raise ContainerError(
                f"cannot register {spec.name!r} after startup; the build order is fixed once "
                "services are running, and a late registration would never be started"
            )
        if spec.name in self._specs:
            raise ContainerError(f"service {spec.name!r} is already registered")
        self._specs[spec.name] = spec
        self._status[spec.name] = ServiceStatus(
            name=spec.name, state=ServiceState.REGISTERED, critical=spec.critical
        )
        return self

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._specs))

    def spec(self, name: str) -> ServiceSpec:
        found = self._specs.get(name)
        if found is None:
            raise ContainerError(
                f"unknown service {name!r}; registered: {', '.join(self.names()) or '(none)'}"
            )
        return found

    def build_order(self) -> tuple[str, ...]:
        """Dependencies before dependents, deterministic, with cycles refused.

        Sorted at each level so the order is reproducible: a build order that varies between
        processes makes a startup failure impossible to reproduce.
        """
        missing: list[str] = []
        for spec in self._specs.values():
            missing.extend(
                f"{spec.name} -> {dependency}"
                for dependency in spec.depends_on
                if dependency not in self._specs
            )
        if missing:
            raise ContainerError(
                "services declare dependencies that are not registered: "
                + ", ".join(sorted(missing))
            )

        ordered: list[str] = []
        visiting: list[str] = []
        seen: set[str] = set()

        def visit(name: str) -> None:
            if name in seen:
                return
            if name in visiting:
                cycle = " -> ".join([*visiting[visiting.index(name) :], name])
                raise ContainerError(
                    f"dependency cycle: {cycle}. A container that cannot detect this recurses "
                    "until the stack runs out and blames itself rather than the cycle."
                )
            visiting.append(name)
            for dependency in sorted(self._specs[name].depends_on):
                visit(dependency)
            visiting.pop()
            seen.add(name)
            ordered.append(name)

        for name in sorted(self._specs):
            visit(name)
        return tuple(ordered)

    def get(self, name: str) -> Any:
        """Resolve a service, building it if necessary.

        Building on demand means a factory can call ``get`` for its own dependencies, so the
        dependency list in the spec and the calls in the factory cannot drift apart — the
        second is what actually builds, and the first is checked against it.
        """
        if name in self._instances:
            return self._instances[name]

        spec = self.spec(name)
        if name in self._resolving:
            cycle = " -> ".join([*self._resolving[self._resolving.index(name) :], name])
            raise ContainerError(f"dependency cycle while resolving: {cycle}")

        self._resolving.append(name)
        started = time.perf_counter()
        try:
            instance = spec.factory(self)
        finally:
            self._resolving.pop()

        elapsed = (time.perf_counter() - started) * 1000
        self._instances[name] = instance
        self._status[name] = ServiceStatus(
            name=name, state=ServiceState.STARTED, critical=spec.critical, build_ms=elapsed
        )
        return instance

    def try_get(self, name: str) -> Any | None:
        """Resolve, or ``None`` if the service is degraded or absent.

        The accessor a caller uses for a non-critical dependency. Distinguishing "absent" from
        "raised" at the call site is what lets a feature degrade rather than propagate.
        """
        status = self._status.get(name)
        if status is None or status.state in (ServiceState.DEGRADED, ServiceState.FAILED):
            return None
        try:
            return self.get(name)
        except ContainerError:
            return None

    def start(self) -> StartupReport:
        """Build every service in dependency order.

        A critical failure **aborts immediately**. Continuing would build the remaining services
        against a platform that is already known to be broken, and the resulting error report
        names whichever service failed last rather than the one that actually failed.
        """
        if self._started:
            raise ContainerError("the container is already started")

        began = time.perf_counter()
        order = self.build_order()
        self._order = order
        statuses: list[ServiceStatus] = []
        aborted = ""

        for name in order:
            spec = self._specs[name]
            unmet = [
                dependency
                for dependency in spec.depends_on
                if not self._status[dependency].state.is_usable
            ]
            if unmet:
                # A dependent of a degraded service is itself degraded, whatever it declared.
                # Building it would produce an object holding a None it does not expect.
                status = ServiceStatus(
                    name=name,
                    state=ServiceState.DEGRADED if not spec.critical else ServiceState.FAILED,
                    critical=spec.critical,
                    error=f"dependency unavailable: {', '.join(sorted(unmet))}",
                )
                self._status[name] = status
                statuses.append(status)
                if spec.critical:
                    aborted = name
                    break
                continue

            try:
                self.get(name)
                statuses.append(self._status[name])
            except Exception as exc:
                state = ServiceState.FAILED if spec.critical else ServiceState.DEGRADED
                status = ServiceStatus(
                    name=name,
                    state=state,
                    critical=spec.critical,
                    error=f"{type(exc).__name__}: {exc}",
                )
                self._status[name] = status
                statuses.append(status)
                _log.error(
                    "container.service_failed",
                    service=name,
                    critical=spec.critical,
                    error=type(exc).__name__,
                )
                if spec.critical:
                    aborted = name
                    break

        self._started = True
        report = StartupReport(
            started=tuple(statuses),
            order=order,
            duration_ms=(time.perf_counter() - began) * 1000,
            aborted_on=aborted,
        )
        _log.info(
            "container.started",
            ok=report.ok,
            services=len(statuses),
            degraded=len(report.degraded),
            duration_ms=round(report.duration_ms, 1),
        )
        return report

    def stop(self) -> tuple[str, ...]:
        """Tear down in reverse build order.

        Reverse, because stopping a dependency before its dependents makes the dependents fail
        on the way down and the error log then describes the wrong component. A stop hook that
        raises is logged and does not prevent the remaining services stopping — a shutdown that
        aborts halfway leaks whatever it had not reached yet.
        """
        stopped: list[str] = []
        for name in reversed(self._order or self.build_order()):
            spec = self._specs.get(name)
            instance = self._instances.get(name)
            if spec is None or instance is None:
                continue
            if spec.stop is not None:
                try:
                    spec.stop(instance)
                except Exception as exc:
                    _log.error("container.stop_failed", service=name, error=type(exc).__name__)
            # Every service torn down, not only the hooked ones: reporting only the hooked
            # ones made a clean shutdown of ten services print "0 stopped", which reads as a
            # shutdown that did nothing.
            stopped.append(name)
        self._instances.clear()
        for name in self._status:
            self._status[name] = ServiceStatus(
                name=name,
                state=ServiceState.STOPPED,
                critical=self._specs[name].critical,
            )
        self._started = False
        return tuple(stopped)

    def status(self, name: str) -> ServiceStatus:
        found = self._status.get(name)
        if found is None:
            raise ContainerError(f"unknown service {name!r}")
        return found

    def statuses(self) -> tuple[ServiceStatus, ...]:
        return tuple(self._status[name] for name in self._order or self.names())

    @property
    def is_started(self) -> bool:
        return self._started

    def dependency_graph(self) -> dict[str, list[str]]:
        """The declared graph, for documentation and for the boundary test."""
        return {name: sorted(spec.depends_on) for name, spec in sorted(self._specs.items())}

    def statistics(self) -> dict[str, Any]:
        return {
            "registered": len(self._specs),
            "built": len(self._instances),
            "started": sum(1 for s in self._status.values() if s.state is ServiceState.STARTED),
            "degraded": sum(1 for s in self._status.values() if s.state is ServiceState.DEGRADED),
            "failed": sum(1 for s in self._status.values() if s.state is ServiceState.FAILED),
        }


@dataclass(slots=True)
class ContainerBuilder:
    """Collects specs before handing over a container.

    Exists so a deployment can compose its own service set — a test wants three services, a
    production process wants all of them — without the registration list being a module-level
    constant that every caller inherits.
    """

    specs: list[ServiceSpec] = field(default_factory=list)

    def add(
        self,
        name: str,
        factory: Callable[[ServiceContainer], Any],
        *,
        depends_on: tuple[str, ...] = (),
        critical: bool = True,
        description: str = "",
        stop: Callable[[Any], None] | None = None,
    ) -> ContainerBuilder:
        self.specs.append(
            ServiceSpec(
                name=name,
                factory=factory,
                depends_on=depends_on,
                critical=critical,
                description=description,
                stop=stop,
            )
        )
        return self

    def build(self) -> ServiceContainer:
        container = ServiceContainer()
        for spec in self.specs:
            container.register(spec)
        return container
