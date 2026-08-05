"""The Phase 8 verification run.

Starts the whole platform, walks a document from upload to a recorded analytics fact, serves
real HTTP requests against the mounted APIs, and validates the deployment assets — in one
process, with nothing stubbed and nothing skipped silently.

Docker, Kubernetes, PostgreSQL, MongoDB, Neo4j, and Redis are not available in every environment
this runs in, and this deliberately does not pretend otherwise. What can run in-process runs;
what needs infrastructure is *named as unverified* rather than reported as passing. A
verification run that claims a green deploy it never attempted is worse than one that admits
the gap.

Run it with ``python -m cip_gateway.demo``.
"""

from __future__ import annotations

import asyncio
import time
import uuid

from cip_gateway.container import ServiceContainer
from cip_gateway.deployment import validate_deployment
from cip_gateway.pipeline import ClinicalPipeline
from cip_gateway.platform import build_platform
from cip_gateway.routes import platform_routes
from cip_gateway.startup import validate_startup

_DOCUMENT = b"""DISCHARGE SUMMARY

Patient: [REDACTED]   MRN: 88213   Admitted: 2026-03-02   Discharged: 2026-03-09

HISTORY OF PRESENT ILLNESS
A 71-year-old presented with three days of exertional dyspnoea and bilateral ankle oedema.
Prior myocardial infarction in 2019. Known chronic kidney disease, stage 3.

HOSPITAL COURSE
Treated for acute decompensated heart failure with intravenous furosemide. Echocardiogram
showed an ejection fraction of 32%. Creatinine rose from 1.4 to 1.9 mg/dL during diuresis and
settled at 1.6 before discharge. Started on sacubitril/valsartan and carvedilol.

DISCHARGE MEDICATIONS
Sacubitril/valsartan 24/26 mg twice daily. Carvedilol 3.125 mg twice daily.
Furosemide 40 mg daily. Atorvastatin 40 mg nightly.

FOLLOW-UP
Cardiology in seven days with repeat renal function and electrolytes.
"""


def _rule(title: str) -> None:
    print(f"\n{title}\n{'-' * len(title)}")


def _startup() -> ServiceContainer:
    _rule("1. Platform startup")
    began = time.perf_counter()
    container = build_platform()
    validation = validate_startup(container)
    print(validation.render())
    if not validation.ok:
        raise SystemExit("startup validation failed; refusing to continue")
    print(f"\n  ready in {(time.perf_counter() - began) * 1000:.0f} ms")
    return container


def _dependency_graph(container: ServiceContainer) -> None:
    _rule("2. Service dependency graph")
    graph = container.dependency_graph()
    for name in container.build_order():
        needs = graph.get(name) or []
        print(f"  {name:<18} <- {', '.join(needs) if needs else '(nothing)'}")


def _routes(container: ServiceContainer) -> None:
    _rule("3. HTTP surface")
    registry = platform_routes()
    issues = registry.validate(container)
    by_service: dict[str, int] = {}
    for route in registry.routes:
        by_service[route.service] = by_service.get(route.service, 0) + 1
    for service, count in sorted(by_service.items()):
        print(f"  {service:<18} {count} route(s)")
    print(f"\n  {len(registry.routes)} routes, {len(issues)} issue(s)")
    for issue in issues:
        print(f"    {issue.render()}")


async def _pipeline(container: ServiceContainer) -> None:
    _rule("4. End-to-end clinical workflow")
    pipeline = ClinicalPipeline(container)
    result = await pipeline.run(
        document=_DOCUMENT,
        media_type="text/plain",
        filename="discharge-summary.txt",
        tenant_id=uuid.uuid4(),
    )
    print(result.render())


def _http(container: ServiceContainer) -> None:
    _rule("5. Live HTTP requests against the mounted APIs")
    from fastapi.testclient import TestClient

    from cip_gateway.app import build_app
    from cip_platform.security.identity import Role, issue_api_key

    client = TestClient(
        build_app(container=container, validate=False), raise_server_exceptions=False
    )
    store = container.get("gateway")["api_keys"]

    keys: dict[str, str] = {}
    for role in (Role.CLINICIAN, Role.RESEARCHER):
        secret, record = issue_api_key(
            tenant_id=uuid.uuid4(),
            roles=frozenset({role}),
            pepper="development-only-pepper",
            display_name=role.value,
        )
        store.add(record)
        keys[role.value] = secret

    org = {"x-organization-id": "org-demo"}
    calls = [
        ("anonymous", "GET /v1/fhir/metadata", lambda: client.get("/v1/fhir/metadata")),
        (
            "anonymous",
            "GET /v1/analytics/metrics",
            lambda: client.get("/v1/analytics/metrics"),
        ),
        (
            "researcher",
            "GET /v1/analytics/metrics",
            lambda: client.get(
                "/v1/analytics/metrics", headers={**org, "x-api-key": keys["researcher"]}
            ),
        ),
        (
            "researcher",
            "GET /v1/fhir/Patient/p1",
            lambda: client.get(
                "/v1/fhir/Patient/p1",
                headers={**org, "x-api-key": keys["researcher"], "x-purpose-of-use": "TREAT"},
            ),
        ),
        (
            "clinician",
            "GET /v1/fhir/Patient/p1 (no purpose)",
            lambda: client.get(
                "/v1/fhir/Patient/p1", headers={**org, "x-api-key": keys["clinician"]}
            ),
        ),
        (
            "clinician",
            "GET /v1/analytics/metrics",
            lambda: client.get(
                "/v1/analytics/metrics", headers={**org, "x-api-key": keys["clinician"]}
            ),
        ),
    ]

    expectations = {
        ("anonymous", "GET /v1/fhir/metadata"): "conformance discovery is public",
        ("anonymous", "GET /v1/analytics/metrics"): "no credential",
        ("researcher", "GET /v1/analytics/metrics"): "has analytics:read",
        ("researcher", "GET /v1/fhir/Patient/p1"): "no patients:read, by design",
        ("clinician", "GET /v1/fhir/Patient/p1 (no purpose)"): "purpose is never inferred",
        ("clinician", "GET /v1/analytics/metrics"): "no analytics:read",
    }
    for principal, label, call in calls:
        response = call()
        note = expectations[(principal, label)]
        print(f"  {principal:<11} {label:<38} {response.status_code}   {note}")


def _deployment() -> None:
    _rule("6. Deployment assets (static)")
    report = validate_deployment()
    print(report.render())


def _infrastructure() -> None:
    _rule("7. What this run did not verify")
    print(
        "  These need infrastructure that is not present here. They are listed rather than\n"
        "  assumed: every one of them is a real gate before a real deploy.\n"
        "\n"
        "    docker build / compose up      a Docker daemon\n"
        "    kubectl apply --dry-run        a cluster and its admission controllers\n"
        "    image vulnerability scan       a registry and a scanner\n"
        "    PostgreSQL row-level security  a live PostgreSQL (CIP_RUN_INTEGRATION=1)\n"
        "    MongoDB Atlas vector search    an Atlas cluster\n"
        "    Neo4j graph traversal          a live Neo4j\n"
        "    Redis-backed rate limiting     a live Redis; the in-process limiter is per replica\n"
        "\n"
        "  Everything above this line ran for real, in this process, against the committed\n"
        "  configuration."
    )


def main() -> None:
    print("Clinical Intelligence Platform — Phase 8 integration verification")
    container = _startup()
    _dependency_graph(container)
    _routes(container)
    asyncio.run(_pipeline(container))
    _http(container)
    _deployment()
    _infrastructure()

    stopped = container.stop()
    _rule("8. Shutdown")
    print(f"  {len(stopped)} service(s) stopped in reverse dependency order")
    print(f"  {' -> '.join(stopped)}")


if __name__ == "__main__":
    main()
