"""Phase 8 integration tests.

Every test here corresponds to a defect that survived the phase that introduced it. That is the
selection criterion, and it is deliberate: each service's own suite passed throughout, because
each service was individually correct. The failures were all in the joins — a package the image
did not copy, two settings systems disagreeing about the word "production", a Secret mounted
where nothing read it, an API nothing routed to.

The tests are therefore mostly *cross-artefact*: they read the Dockerfile, the ConfigMap, and the
Secret manifest and assert that what those files say matches what the code does. A test that
only exercises Python cannot catch a deployment that ships the wrong files.
"""

from __future__ import annotations

import pathlib
import uuid

import pytest
import yaml
from fastapi.testclient import TestClient

from cip_core.config import Environment as CoreEnvironment
from cip_core.secrets import SECRET_FILE_MAP, load_mounted_secrets
from cip_gateway.app import build_app
from cip_gateway.container import ContainerBuilder
from cip_gateway.deployment import Severity, validate_deployment
from cip_gateway.platform import SERVICE_NAMES, build_platform
from cip_gateway.routes import (
    INTERNAL_SERVICES,
    HttpMethod,
    IssueKind,
    RouteSpec,
    platform_routes,
)
from cip_gateway.startup import CheckStatus, StartupError, validate_startup
from cip_platform.config import Environment as PlatformEnvironment
from cip_platform.security.identity import Role, Scope, issue_api_key

REPO = pathlib.Path(__file__).resolve().parents[2]
PEPPER = "development-only-pepper"


# ---------------------------------------------------------------------------------------
# Deployment assets
# ---------------------------------------------------------------------------------------


def test_image_copies_every_source_package() -> None:
    """The Dockerfile listed six packages while the repository had nine.

    The image built, started, and passed its health check with the decision, interop, and
    analytics services absent — three phases of work that could not be imported in production.
    Nothing detected it because the Dockerfile was syntactically valid and no test ran inside
    the image.
    """
    report = validate_deployment(REPO)
    drift = [
        finding for finding in report.findings if "not copied into the image" in finding.detail
    ]
    assert not drift, drift[0].detail if drift else ""


def test_deployment_assets_have_no_blocking_findings() -> None:
    report = validate_deployment(REPO)
    assert report.ok, "\n".join(f.render() for f in report.blocking)


def test_unrunnable_checks_are_reported_not_assumed() -> None:
    """A validator that silently skips the build must not read as a passing build."""
    report = validate_deployment(REPO)
    unverified = {finding.asset for finding in report.unverified}
    assert "docker build" in unverified
    assert "kubectl apply --dry-run=server" in unverified
    assert all(f.severity is Severity.UNVERIFIED for f in report.unverified)


# ---------------------------------------------------------------------------------------
# Configuration: the two settings systems must agree
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    ["production", "prod", "development", "dev", "staging", "test", "testing", "local"],
)
def test_both_settings_systems_accept_the_same_environment_vocabulary(value: str) -> None:
    """``cip_core`` wanted ``prod``; ``cip_platform`` wanted ``production``.

    Both read ``CIP_ENVIRONMENT``. Every deployment asset in the repository set the long form,
    so the image, the compose stack, and the ConfigMap all set a value one of the two rejected —
    and every containerised start failed at settings load. No test set the variable, so no test
    saw it.
    """
    assert CoreEnvironment(value) in set(CoreEnvironment)
    assert PlatformEnvironment(value) in set(PlatformEnvironment)


def test_configmap_satisfies_both_settings_systems() -> None:
    """The ConfigMap configured ``cip_platform`` only.

    ``cip_core``'s deployed-environment validation then refused the configuration — correctly,
    since log format, TLS mode, auth mode, and storage backend were all still development
    defaults. The pod could not start.
    """
    data = yaml.safe_load((REPO / "deploy/k8s/01-configmap.yaml").read_text())["data"]
    assert data["CIP_ENVIRONMENT"] == "production"
    # One assertion per clause of Settings._refuse_unsafe_deployment.
    assert data["CIP_LOG_FORMAT"] == "json"
    assert data["CIP_POSTGRES__SSL_MODE"] == "verify-full"
    assert data["CIP_AUTH__MODE"] == "oidc"
    assert data["CIP_AUTH__JWKS_URL"]
    assert data["CIP_STORAGE__BACKEND"] == "s3"
    assert data["CIP_STORAGE__S3_BUCKET"]
    assert data.get("CIP_DEBUG", "false") == "false"


def test_secret_manifest_keys_all_map_to_a_real_setting() -> None:
    """The manifest declared three keys no settings field corresponds to.

    ``postgres-dsn``, ``api-key-pepper``, and ``jwt-public-key`` mounted successfully, were read
    by nothing, and left the real settings on their defaults. This is the correspondence that
    was missing: manifest key -> mapped variable -> accepted field.
    """
    declared = set(yaml.safe_load((REPO / "deploy/k8s/02-secrets.yaml").read_text())["stringData"])
    mapped = set(SECRET_FILE_MAP)
    assert declared == mapped, {
        "in the manifest, read by nothing": sorted(declared - mapped),
        "expected by the loader, not mounted": sorted(mapped - declared),
    }


def test_mounted_secrets_reach_the_environment(tmp_path: pathlib.Path) -> None:
    """The Deployment mounted a Secret directory and nothing read it.

    Every secret-derived setting therefore held its default in production: the database
    password, the Redis URL, the broker URL, the de-identification salt.
    """
    mount = tmp_path / "cip"
    mount.mkdir()
    for name in SECRET_FILE_MAP:
        (mount / name).write_text(f"value-for-{name}\n", encoding="utf-8")
    # A projected Secret carries these; they must not be read as secrets.
    (mount / "..data").write_text("ignored", encoding="utf-8")

    environ: dict[str, str] = {}
    report = load_mounted_secrets(mount, environ=environ)

    assert report.present
    assert set(report.applied) == set(SECRET_FILE_MAP.values())
    assert environ["CIP_ANALYTICS_SALT"] == "value-for-analytics-salt"
    assert not report.unmapped


def test_an_explicit_variable_beats_a_mounted_file(tmp_path: pathlib.Path) -> None:
    """So an operator can override one value without unmounting the Secret."""
    mount = tmp_path / "cip"
    mount.mkdir()
    (mount / "analytics-salt").write_text("from-the-mount", encoding="utf-8")

    environ = {"CIP_ANALYTICS_SALT": "from-the-operator"}
    report = load_mounted_secrets(mount, environ=environ)

    assert environ["CIP_ANALYTICS_SALT"] == "from-the-operator"
    assert "CIP_ANALYTICS_SALT" in report.already_set


def test_a_secret_nothing_reads_is_reported(tmp_path: pathlib.Path) -> None:
    mount = tmp_path / "cip"
    mount.mkdir()
    (mount / "some-forgotten-key").write_text("x", encoding="utf-8")
    assert load_mounted_secrets(mount, environ={}).unmapped == ("some-forgotten-key",)


# ---------------------------------------------------------------------------------------
# The container
# ---------------------------------------------------------------------------------------


def test_the_whole_platform_starts() -> None:
    report = build_platform().start()
    assert report.ok, report.render()
    assert {status.name for status in report.started} == set(SERVICE_NAMES)


def test_a_dependency_cycle_is_refused_with_the_cycle_in_the_message() -> None:
    container = (
        ContainerBuilder()
        .add("a", lambda c: c.get("b"), depends_on=("b",))
        .add("b", lambda c: c.get("a"), depends_on=("a",))
        .build()
    )
    with pytest.raises(Exception, match=r"cycle|circular"):
        container.build_order()


def test_a_failed_non_critical_service_degrades_rather_than_aborting() -> None:
    """Analytics is non-critical on purpose: an empty warehouse is a reporting gap, and
    refusing to serve clinicians over it would be the wrong trade."""

    def explode(_: object) -> object:
        raise RuntimeError("warehouse unreachable")

    container = (
        ContainerBuilder()
        .add("core", lambda _: object())
        .add("reporting", explode, critical=False)
        .build()
    )
    report = container.start()

    assert report.ok
    assert [s.name for s in report.degraded] == ["reporting"]
    assert container.try_get("reporting") is None
    assert container.try_get("core") is not None


def test_a_failed_critical_service_aborts_startup() -> None:
    def explode(_: object) -> object:
        raise RuntimeError("no")

    container = ContainerBuilder().add("essential", explode, critical=True).build()
    report = container.start()

    assert not report.ok
    assert report.aborted_on == "essential"


def test_services_stop_in_reverse_dependency_order() -> None:
    stopped: list[str] = []
    container = (
        ContainerBuilder()
        .add("base", lambda _: "base", stop=lambda _: stopped.append("base"))
        .add(
            "leaf",
            lambda c: c.get("base"),
            depends_on=("base",),
            stop=lambda _: stopped.append("leaf"),
        )
        .build()
    )
    container.start()
    container.stop()

    assert stopped.index("leaf") < stopped.index("base"), (
        "a service was torn down while something depending on it was still draining"
    )


# ---------------------------------------------------------------------------------------
# The route registry
# ---------------------------------------------------------------------------------------


def test_the_declared_surface_agrees_with_the_running_services() -> None:
    issues = platform_routes().validate(build_platform())
    assert not issues, "\n".join(issue.render() for issue in issues)


def test_a_literal_path_registered_after_a_template_is_reported() -> None:
    """``/v1/fhir/$export`` and ``/v1/fhir/{resource_type}`` normalise differently, so a
    shape comparison sees no conflict — but the router matches in registration order and the
    template swallows the operation. It reaches ``search(resource_type="$export")``, which
    answers a plausible 404 for an unknown resource type. Nothing errors; the endpoint is
    simply gone.

    Found in this repository's own FHIR routes on the check's first run.
    """
    registry = (
        platform_routes()
        .add(RouteSpec(HttpMethod.GET, "/v1/things/{thing_id}", "interop", "read_thing"))
        .add(RouteSpec(HttpMethod.GET, "/v1/things/summary", "interop", "summary"))
    )
    kinds = [issue.kind for issue in registry.validate()]
    assert IssueKind.SHADOWED in kinds


def test_the_real_fhir_routes_put_literals_before_templates() -> None:
    paths = [r.path for r in platform_routes().for_service("interop")]
    for literal in ("/v1/fhir/metadata", "/v1/fhir/$export", "/v1/fhir/$import"):
        assert paths.index(literal) < paths.index("/v1/fhir/{resource_type}")


def test_two_routes_on_the_same_method_and_path_are_reported() -> None:
    registry = platform_routes().add(
        RouteSpec(HttpMethod.GET, "/v1/analytics/metrics", "analytics", "duplicate")
    )
    assert IssueKind.DUPLICATE in [issue.kind for issue in registry.validate()]


def test_paths_differing_only_in_parameter_name_are_reported() -> None:
    registry = platform_routes().add(
        RouteSpec(HttpMethod.GET, "/v1/analytics/metrics/{slug}", "analytics", "alias")
    )
    assert IssueKind.SHADOWED in [issue.kind for issue in registry.validate()]


def test_a_route_for_an_unregistered_service_is_reported() -> None:
    registry = platform_routes().add(RouteSpec(HttpMethod.GET, "/v1/billing", "billing", "invoice"))
    issues = registry.validate(build_platform())
    assert IssueKind.DEAD in [issue.kind for issue in issues]


def test_a_registered_service_with_no_route_is_reported() -> None:
    """Phase 6 and Phase 7 each shipped a complete API this way: implemented, unit-tested,
    and reachable only from Python."""
    container = (
        ContainerBuilder().add("settings", lambda _: {}).add("orphan", lambda _: object()).build()
    )
    issues = platform_routes().validate(container)
    unreachable = [i for i in issues if i.kind is IssueKind.UNREACHABLE]
    assert any("orphan" in issue.detail for issue in unreachable)


def test_every_service_is_either_routed_or_declared_internal() -> None:
    registered = set(build_platform().names())
    assert registered <= platform_routes().services() | set(INTERNAL_SERVICES)


def test_an_anonymous_route_must_state_why() -> None:
    registry = platform_routes().add(
        RouteSpec(HttpMethod.GET, "/v1/open", "analytics", "open", authenticated=False)
    )
    assert IssueKind.UNAUTHENTICATED in [issue.kind for issue in registry.validate()]


# ---------------------------------------------------------------------------------------
# Startup validation
# ---------------------------------------------------------------------------------------


def test_development_starts_with_warnings_not_failures() -> None:
    validation = validate_startup(build_platform(), production=False)
    assert validation.ok, validation.render()


def test_production_refuses_a_default_deidentification_salt(monkeypatch: object) -> None:
    """A salt in the repository is a salt everyone knows.

    Pseudonyms are ``HMAC(salt, identifier)``, so a known salt lets anyone holding a list of
    candidate MRNs recompute every key in the warehouse and re-identify a dataset built
    specifically to be de-identifiable.
    """
    import os

    saved = os.environ.pop("CIP_ANALYTICS_SALT", None)
    try:
        validation = validate_startup(build_platform(), production=True, start_services=False)
        salt_checks = [c for c in validation.checks if c.name.endswith("CIP_ANALYTICS_SALT")]
        assert salt_checks and salt_checks[0].status is CheckStatus.FAILED
        with pytest.raises(StartupError):
            validation.raise_for_status()
    finally:
        if saved is not None:
            os.environ["CIP_ANALYTICS_SALT"] = saved


def test_validation_reports_every_failure_not_just_the_first() -> None:
    """An operator restarting on each successive error learns one problem per crash loop."""
    validation = validate_startup(build_platform(), production=True, start_services=False)
    assert len(validation.checks) > 1


# ---------------------------------------------------------------------------------------
# The HTTP surface
# ---------------------------------------------------------------------------------------


@pytest.fixture
def client_and_keys() -> tuple[TestClient, dict[str, str]]:
    container = build_platform()
    app = build_app(container=container)
    store = container.get("gateway")["api_keys"]

    keys: dict[str, str] = {}
    for role in (Role.CLINICIAN, Role.RESEARCHER):
        secret, record = issue_api_key(
            tenant_id=uuid.uuid4(), roles=frozenset({role}), pepper=PEPPER, display_name=role.value
        )
        store.add(record)
        keys[role.value] = secret
    return TestClient(app, raise_server_exceptions=False), keys


def test_the_interop_and_analytics_apis_are_actually_mounted(
    client_and_keys: tuple[TestClient, dict[str, str]],
) -> None:
    client, _ = client_and_keys
    mounted = {r.name for r in client.app.routes if getattr(r, "name", "").count(".")}
    assert "interop.capability" in mounted
    assert "analytics.list_metrics" in mounted


def test_capability_is_anonymous_and_reachable(
    client_and_keys: tuple[TestClient, dict[str, str]],
) -> None:
    """Not shadowed by ``/v1/fhir/{resource_type}``, and not gated behind a purpose of use:
    it is service discovery, and every FHIR client reads it before its first real request."""
    client, _ = client_and_keys
    response = client.get("/v1/fhir/metadata")
    assert response.status_code == 200
    assert response.json()["resourceType"] == "CapabilityStatement"


def test_a_disclosure_without_a_stated_purpose_is_refused(
    client_and_keys: tuple[TestClient, dict[str, str]],
) -> None:
    """An adapter that supplies a sensible default here has silently disabled the consent
    engine (docs/design/adr-0028-consent-deny-by-default.md)."""
    client, keys = client_and_keys
    response = client.get(
        "/v1/fhir/Patient/p1",
        headers={"x-api-key": keys["clinician"], "x-organization-id": "org-1"},
    )
    assert response.status_code == 400
    assert "purpose" in response.json()["title"]


def test_an_unknown_purpose_is_refused(
    client_and_keys: tuple[TestClient, dict[str, str]],
) -> None:
    client, keys = client_and_keys
    response = client.get(
        "/v1/fhir/Patient/p1",
        headers={
            "x-api-key": keys["clinician"],
            "x-organization-id": "org-1",
            "x-purpose-of-use": "BECAUSE-I-SAID-SO",
        },
    )
    assert response.status_code == 400


def test_an_authenticated_route_refuses_an_anonymous_caller(
    client_and_keys: tuple[TestClient, dict[str, str]],
) -> None:
    client, _ = client_and_keys
    assert client.get("/v1/analytics/metrics").status_code == 401


def test_scope_is_enforced_per_route(
    client_and_keys: tuple[TestClient, dict[str, str]],
) -> None:
    """A researcher has ``analytics:read`` and deliberately not ``patients:read`` — a
    researcher who can read an identified record has defeated the de-identification. The
    clinician holds the mirror image. Both directions are asserted, because a check that only
    tests the allowed direction passes when authorisation is disabled entirely.
    """
    client, keys = client_and_keys
    org = {"x-organization-id": "org-1"}
    researcher = {**org, "x-api-key": keys["researcher"]}
    clinician = {**org, "x-api-key": keys["clinician"], "x-purpose-of-use": "TREAT"}

    assert client.get("/v1/analytics/metrics", headers=researcher).status_code == 200
    assert (
        client.get(
            "/v1/fhir/Patient/p1", headers=researcher | {"x-purpose-of-use": "TREAT"}
        ).status_code
        == 403
    )

    assert client.get("/v1/analytics/metrics", headers=clinician).status_code == 403
    # 404, not 403: authorised, and the resource genuinely is not there.
    assert client.get("/v1/fhir/Patient/p1", headers=clinician).status_code == 404


def test_every_route_declares_a_scope() -> None:
    for route in platform_routes().routes:
        assert isinstance(route.scope, Scope), route.key


def test_a_degraded_service_answers_503_not_500() -> None:
    """The request is fine and the dependency is not, and the difference decides whether a
    client should retry."""

    from cip_gateway.platform import _gateway, _settings

    def explode(_: object) -> object:
        raise RuntimeError("warehouse unreachable")

    # Degrade it the way the container would on its own, rather than by writing to internals:
    # a real factory that raises, registered non-critical.
    container = (
        ContainerBuilder()
        .add("settings", _settings)
        .add("gateway", _gateway, depends_on=("settings",))
        .add("analytics", explode, depends_on=("settings",), critical=False)
        .build()
    )
    report = container.start()
    assert report.ok and [s.name for s in report.degraded] == ["analytics"]
    assert container.try_get("analytics") is None

    client = TestClient(
        build_app(container=container, validate=False), raise_server_exceptions=False
    )
    secret, record = issue_api_key(
        tenant_id=uuid.uuid4(), roles=frozenset({Role.RESEARCHER}), pepper=PEPPER
    )
    container.get("gateway")["api_keys"].add(record)

    response = client.get(
        "/v1/analytics/metrics", headers={"x-api-key": secret, "x-organization-id": "org-1"}
    )
    assert response.status_code == 503


# ---------------------------------------------------------------------------------------
# The architectural invariant the whole phase rests on
# ---------------------------------------------------------------------------------------


class TestServiceBoundaries:
    """Only the composition root may know about more than one service.

    This is the property that makes the container worth having. Six services that do not import
    each other can be reasoned about, tested, and replaced independently; the moment a seventh
    place starts wiring them together, that value is gone and no amount of dependency injection
    brings it back. So it is asserted rather than intended.
    """

    SERVICES = ("ingestion", "retrieval", "copilot", "decision", "interop", "analytics")

    #: The modules permitted to import more than one service, with why.
    COMPOSITION_ROOT = {
        "platform.py": "the registration list — its entire job is knowing every service",
        "app.py": "the HTTP adapter, which routes to the services the registry names",
        "pipeline.py": "the end-to-end workflow, which is by definition cross-service",
        "demo.py": "the verification run",
    }

    def _gateway_root(self) -> pathlib.Path:
        return REPO / "services/gateway/src/cip_gateway"

    def test_only_the_composition_root_imports_more_than_one_service(self) -> None:
        import re

        pattern = re.compile(r"\bcip_(" + "|".join(self.SERVICES) + r")\b")
        violations: list[str] = []
        for path in sorted(self._gateway_root().rglob("*.py")):
            if path.name in self.COMPOSITION_ROOT:
                continue
            touched = set(pattern.findall(path.read_text(encoding="utf-8")))
            if len(touched) > 1:
                violations.append(f"{path.name} imports {sorted(touched)}")
        assert not violations, (
            "modules outside the composition root reach into several services:\n"
            + "\n".join(violations)
        )

    #: The service stack. A service may import a strictly lower layer and nothing else.
    #:
    #: The document path is a genuine pipeline — ingestion produces chunks, retrieval embeds and
    #: searches them, the copilot answers over what retrieval returns — so those dependencies are
    #: real and directional, and pretending otherwise would mean copying types between them.
    #: Decision, interop, and analytics sit at the bottom because they depend on no other
    #: service: they are reached through the container, never by import.
    #:
    #: Sideways is banned along with upward. Two services at the same layer importing each other
    #: is how a stack becomes a graph, and a graph has no safe start-up order.
    LAYERS = {
        "ingestion": 1,
        "decision": 1,
        "interop": 1,
        "analytics": 1,
        "retrieval": 2,
        "copilot": 3,
    }

    def test_no_service_imports_upward_or_sideways(self) -> None:
        import re

        pattern = re.compile(r"\bcip_(" + "|".join(self.SERVICES) + r")\b")
        violations: list[str] = []
        for service, own in sorted(self.LAYERS.items()):
            root = REPO / f"services/{service}/src/cip_{service}"
            if not root.is_dir():
                continue
            for path in sorted(root.rglob("*.py")):
                for imported in set(pattern.findall(path.read_text(encoding="utf-8"))):
                    if imported == service:
                        continue
                    other = self.LAYERS[imported]
                    if other >= own:
                        violations.append(
                            f"{service} (layer {own}) imports {imported} (layer {other}) "
                            f"in {path.name}"
                        )
        assert not violations, "upward or sideways imports:\n" + "\n".join(violations)

    def test_the_bottom_layer_imports_no_other_service(self) -> None:
        """Decision, interop, and analytics are reached through the container, never by import.

        Asserted separately from the layering rule because it is the property that lets the
        container start them in any order and degrade one without touching the others.
        """
        import re

        pattern = re.compile(r"\bcip_(" + "|".join(self.SERVICES) + r")\b")
        violations: list[str] = []
        for service in ("decision", "interop", "analytics"):
            root = REPO / f"services/{service}/src/cip_{service}"
            for path in sorted(root.rglob("*.py")):
                found = {
                    name
                    for name in pattern.findall(path.read_text(encoding="utf-8"))
                    if name != service
                }
                if found:
                    violations.append(f"{service}/{path.name} imports {sorted(found)}")
        assert not violations, "\n".join(violations)

    def test_every_registered_service_is_named_in_one_place(self) -> None:
        """A typo'd name in a health check reports on a service that does not exist, and
        reports nothing about the one that does."""
        assert set(build_platform().names()) == set(SERVICE_NAMES)


def test_routes_with_path_parameters_pass_the_principal_correctly(
    client_and_keys: tuple[TestClient, dict[str, str]],
) -> None:
    """Query parameters were briefly passed positionally into ``get_metric``.

    That put a dict of untrusted query strings where the principal belongs — the argument every
    authorisation decision in the analytics API is made from. Nothing caught it because the
    tests only exercised routes without path parameters; the type checker did.

    So every parameterised route is exercised here, and the assertion is that none of them
    returns a 500: a handler calling its service with the wrong argument shape cannot pass.
    """
    client, keys = client_and_keys
    headers = {"x-organization-id": "org-1", "x-api-key": keys["researcher"]}
    for path in (
        "/v1/analytics/metrics/encounter_count",
        "/v1/analytics/metrics/encounter_count?group_by=cohort",
        "/v1/analytics/dashboards/does-not-exist",
        "/v1/analytics/reports/does-not-exist/runs",
    ):
        response = client.get(path, headers=headers)
        assert response.status_code < 500, f"{path} -> {response.status_code} {response.text[:200]}"


class TestTheAdapterTrustsOnlyTheCredential:
    """Two privilege escalations lived in the HTTP adapter until this review.

    The adapter built the interop request from ``x-organization-id`` and ``x-granted-scopes``
    request headers. Both are client-supplied. The first let any caller name any organisation —
    and the organisation is what consent is evaluated against, so it was a cross-tenant read.
    The second let a caller send ``system/*.read`` and have the consent layer honour it.

    Neither showed up in a test, because every test was sending the headers it was supposed to.
    A test that only exercises the honest client cannot find a bug that needs a dishonest one.
    """

    def _adapter_source(self) -> str:
        return (REPO / "services/gateway/src/cip_gateway/app.py").read_text(encoding="utf-8")

    def test_the_adapter_reads_no_identity_from_request_headers(self) -> None:
        """Matches the access pattern, not the prose.

        An earlier version searched for the header names anywhere in the file and failed on the
        comments explaining why they must not be read — a check that forbids discussing the bug
        is not a check on the bug.
        """
        import re

        pattern = re.compile(
            r"headers(?:\.get\(\s*|\[\s*)[\"']x-(organization-id|granted-scopes|roles|token-issuer)"
        )
        found = pattern.findall(self._adapter_source())
        assert not found, (
            f"the adapter reads identity from client-controlled header(s) {sorted(set(found))}; "
            f"take it from the authenticated Principal instead"
        )

    def test_scopes_come_from_the_principal(self) -> None:
        """A principal gets exactly what its verified platform scopes map to, and nothing more."""
        from cip_gateway.app import _scopes_for

        class _FakePrincipal:
            scopes = frozenset({Scope.ANALYTICS_READ})

        granted = _scopes_for(_FakePrincipal())
        # analytics:read maps to no FHIR scope at all: analytics is de-identified aggregate
        # data and grants nothing over identified records.
        assert granted.scopes == ()

        class _Clinician:
            scopes = frozenset({Scope.PATIENTS_READ})

        assert _scopes_for(_Clinician()).scopes, "patients:read must grant a FHIR read scope"

    def test_a_spoofed_organization_header_changes_nothing(
        self, client_and_keys: tuple[TestClient, dict[str, str]]
    ) -> None:
        """The same credential must behave identically however the caller labels itself."""
        client, keys = client_and_keys
        base = {"x-api-key": keys["researcher"]}
        honest = client.get("/v1/analytics/metrics", headers=base)
        spoofed = client.get(
            "/v1/analytics/metrics",
            headers={**base, "x-organization-id": "some-other-hospital"},
        )
        assert honest.status_code == spoofed.status_code == 200
        assert honest.json() == spoofed.json()

    def test_a_caller_cannot_grant_itself_fhir_scopes(
        self, client_and_keys: tuple[TestClient, dict[str, str]]
    ) -> None:
        """A researcher has no ``patients:read``; asking for it in a header must not help."""
        client, keys = client_and_keys
        response = client.get(
            "/v1/fhir/Patient/p1",
            headers={
                "x-api-key": keys["researcher"],
                "x-purpose-of-use": "TREAT",
                "x-granted-scopes": "system/*.read user/*.read patient/*.read",
            },
        )
        assert response.status_code == 403, response.text[:300]
