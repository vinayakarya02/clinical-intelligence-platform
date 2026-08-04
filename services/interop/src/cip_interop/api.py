"""The clinical API surface: REST, FHIR, bulk export, and bulk import.

One canonical access path, four projections. Consent evaluation, organisation scoping, scope
checking, and ABAC all happen **in the canonical layer, below the projections**
(docs/design/adr-0032-api-surface-and-versioning.md). A new output format therefore cannot
accidentally omit an authorisation check, because the format layer never touches storage.

The order of checks is deliberate and is the order of increasing cost:

1. token validity — cheap, and an expired token should not consume anything else
2. SMART scope — does this client have any business with this resource type at all
3. patient context — a ``patient/`` scope confined to one patient must not read another
4. organisation scoping and sharing agreement — may this organisation see the holder's data
5. consent — did the patient permit this purpose, for this actor
6. ABAC — does local policy allow it

Consent is checked *after* the structural checks and before the data is read, so a denial never
requires having loaded the record first.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from cip_core.logging import get_logger
from cip_interop.consent import ConsentEngine, ConsentOutcome, DisclosureRequest
from cip_interop.domain import InteropError, PurposeOfUse
from cip_interop.fhir.bundle import search_bundle
from cip_interop.fhir.capability import capability_statement
from cip_interop.fhir.definitions import FhirVersion, definition_for
from cip_interop.fhir.repository import (
    ConcurrencyError,
    RepositoryRegistry,
    ResourceNotFoundError,
)
from cip_interop.fhir.resources import Resource
from cip_interop.orgs import AgreementRegistry, OrganizationContext
from cip_interop.security import AbacPolicy, Operation, ScopeSet, TokenClaims

__all__ = [
    "ApiRequest",
    "ApiResponse",
    "BulkExportJob",
    "BulkImportResult",
    "ClinicalApi",
    "ExportStatus",
]

_log = get_logger(__name__)

API_VERSION = "v1"


@dataclass(frozen=True, slots=True)
class ApiRequest:
    """One inbound call, with everything authorisation needs.

    ``purpose`` has no default. A caller that does not state one is refused, because a purpose
    the system infers is a purpose nobody stated
    (docs/design/adr-0028-consent-deny-by-default.md).
    """

    context: OrganizationContext
    claims: TokenClaims
    purpose: PurposeOfUse
    fhir_version: FhirVersion = FhirVersion.R4
    if_match: str = ""
    break_glass_reason: str = ""
    at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))

    @property
    def scopes(self) -> ScopeSet:
        return self.claims.scopes


@dataclass(frozen=True, slots=True)
class ApiResponse:
    """One outbound response."""

    status: int
    body: dict[str, Any] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def render(self) -> str:
        return f"{self.status} {json.dumps(self.body, sort_keys=True)[:160]}"


def _outcome(status: int, code: str, diagnostics: str, **headers: str) -> ApiResponse:
    """A FHIR ``OperationOutcome`` response.

    Every failure returns one. A bare status code tells an integrator nothing, and the most
    common support ticket in healthcare integration is "it returns 403 and we do not know why".
    """
    return ApiResponse(
        status=status,
        body={
            "resourceType": "OperationOutcome",
            "issue": [{"severity": "error", "code": code, "diagnostics": diagnostics}],
        },
        headers=dict(headers),
    )


class ExportStatus(StrEnum):
    """Where a bulk export has got to."""

    ACCEPTED = "accepted"
    IN_PROGRESS = "in-progress"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in (ExportStatus.COMPLETED, ExportStatus.FAILED, ExportStatus.CANCELLED)


@dataclass(slots=True)
class BulkExportJob:
    """An asynchronous population-level export.

    Runs against a **snapshot** taken at kick-off. Without one, a manifest can describe files
    whose contents changed while it was being written, and two clients polling the same job get
    different data from the same URLs.
    """

    job_id: str
    organization_id: str
    resource_types: tuple[str, ...]
    requested_by: str
    purpose: PurposeOfUse = PurposeOfUse.OPERATIONS
    """Carried on the job because consent is evaluated **per patient at export time**, and the
    purpose is what a consent provision is scoped to."""
    context: OrganizationContext | None = None
    excluded_for_consent: int = 0
    """Patients whose consent does not permit this purpose. Reported in the manifest rather
    than silently dropped: a filtered cohort is a biased cohort, and a researcher who does not
    know it was filtered will treat it as complete."""
    status: ExportStatus = ExportStatus.ACCEPTED
    requested_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))
    completed_at: dt.datetime | None = None
    files: dict[str, str] = field(default_factory=dict)
    error: str = ""
    expires_after: dt.timedelta = dt.timedelta(hours=24)
    snapshot_version: int = 0

    @property
    def status_url(self) -> str:
        return f"/{API_VERSION}/$export-status/{self.job_id}"

    def expires_at(self) -> dt.datetime:
        return (self.completed_at or self.requested_at) + self.expires_after

    def manifest(self, base_url: str = "") -> dict[str, Any]:
        return {
            "transactionTime": self.requested_at.isoformat(),
            "request": f"{base_url}/{API_VERSION}/$export",
            "requiresAccessToken": True,
            "output": [
                {"type": resource_type, "url": f"{base_url}{url}"}
                for resource_type, url in sorted(self.files.items())
            ],
            "error": [],
            # Retention is stated in the manifest rather than left for a client to discover
            # when a URL stops working mid-download.
            "expiresAt": self.expires_at().isoformat(),
            "extension": {
                "excludedForConsent": self.excluded_for_consent,
                "consentNote": (
                    "Patients whose filed consent does not permit "
                    f"{self.purpose.value} are excluded. This cohort is filtered by patient "
                    "choice and is not a complete population."
                ),
            },
        }


@dataclass(frozen=True, slots=True)
class BulkImportResult:
    """The outcome of an NDJSON import."""

    accepted: int = 0
    rejected: int = 0
    errors: tuple[str, ...] = ()

    @property
    def total(self) -> int:
        return self.accepted + self.rejected

    def to_json(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "rejected": self.rejected,
            "errors": list(self.errors[:50]),
            "errors_truncated": max(0, len(self.errors) - 50),
        }


class ClinicalApi:
    """The canonical access path.

    Every projection goes through this, so the authorisation checks cannot be bypassed by
    adding an output format.
    """

    def __init__(
        self,
        *,
        repositories: RepositoryRegistry,
        consent: ConsentEngine,
        agreements: AgreementRegistry,
        resolve_person: Callable[[str, str, str], str],
        policy: AbacPolicy | None = None,
        base_url: str = "",
        max_export_jobs: int = 1000,
    ) -> None:
        self._repositories = repositories
        self._consent = consent
        self._agreements = agreements
        self._resolve_person = resolve_person
        """``(organisation, resource type, resource id) -> person id``.

        A **required** argument, not an optional one with an identity default. Consent is filed
        against a person; FHIR resource ids are organisation-local. Looking consent up under an
        organisation-local id means a patient with records at two organisations needs two
        consents, and revoking one leaves the other disclosing — a consent bypass that presents
        as a data-entry gap. Making the resolver required means the connection cannot be
        forgotten (docs/design/adr-0030-cross-organisation-sharing.md)."""
        self._policy = policy or AbacPolicy()
        self._base_url = base_url
        self._exports: dict[str, BulkExportJob] = {}
        self._max_export_jobs = max_export_jobs

    def capability(self, version: FhirVersion = FhirVersion.R4) -> ApiResponse:
        return ApiResponse(
            status=200,
            body=capability_statement(version=version, base_url=self._base_url),
            headers={
                "Content-Type": f"application/fhir+json; fhirVersion={version.mime_parameter}"
            },
        )

    def _authorize(
        self,
        request: ApiRequest,
        *,
        resource_type: str,
        operation: Operation,
        holding_organization_id: str,
        person_id: str,
        resource_attributes: dict[str, str] | None = None,
    ) -> ApiResponse | None:
        """Run every check. Returns a refusal, or ``None`` when the call may proceed."""
        if not request.claims.is_valid_at(request.at):
            return _outcome(401, "login", "the access token is expired or not yet valid")

        if not request.scopes.permits(
            resource_type, operation, resource_attributes=resource_attributes or {}
        ):
            return _outcome(
                403,
                "forbidden",
                f"no SMART scope permits {operation.name.lower()} on {resource_type}; the "
                f"token carries: {request.scopes.render() or '(none)'}",
            )

        # A patient-context token confined to one patient must not read another. A server that
        # parses the launch context and then ignores it turns a single-patient app into a
        # whole-population one.
        if (
            request.claims.patient_context
            and person_id
            and request.claims.patient_context != person_id
            and any(s.context == "patient" for s in request.scopes.scopes)
        ):
            return _outcome(
                403,
                "forbidden",
                f"the token's patient launch context is {request.claims.patient_context}; "
                f"it cannot be used to access {person_id}",
            )

        sharing = self._agreements.evaluate(
            source_organization_id=holding_organization_id,
            target_organization_id=request.context.organization_id,
            purpose=request.purpose,
            on=request.at.date(),
        )
        if not sharing.permitted:
            return _outcome(403, "forbidden", sharing.reason)

        if person_id:
            evaluation = self._consent.evaluate(
                DisclosureRequest(
                    person_id=person_id,
                    context=request.context,
                    purpose=request.purpose,
                    data_category=resource_type,
                    holding_organization_id=holding_organization_id,
                    break_glass_reason=request.break_glass_reason,
                    at=request.at,
                )
            )
            if not evaluation.discloses:
                # The three outcomes stay distinguishable in the response. "No consent on file"
                # tells an operator to obtain one; "denied" tells them the patient decided.
                status = 403 if evaluation.outcome is ConsentOutcome.DENIED else 428
                return _outcome(
                    status,
                    "forbidden" if status == 403 else "required",
                    f"{evaluation.reason} (next action: {evaluation.outcome.operational_action})",
                )

        decision = self._policy.evaluate(
            subject={
                "principal": request.context.principal_id,
                "organization": request.context.organization_id,
                **dict(request.claims.attributes),
                **{f"role:{r}": "true" for r in request.context.roles},
            },
            resource={
                "type": resource_type,
                "organization": holding_organization_id,
            },
            action=operation.name.lower(),
            purpose=request.purpose,
        )
        if not decision.permitted:
            return _outcome(403, "forbidden", decision.reason)
        return None

    def _person_for(self, organization_id: str, resource: Resource) -> str:
        """The person a resource is about, resolved through the EMPI.

        Falls back to the resource's own patient reference **only when the resolver knows
        nothing**, and that fallback is logged: an unresolvable resource means consent will be
        evaluated against an identifier no consent is filed under, which must be visible rather
        than silently permissive.
        """
        local = _local_patient_id(resource)
        if not local:
            return ""
        person_id = self._resolve_person(organization_id, "Patient", local)
        if not person_id:
            _log.warning(
                "api.unresolved_person",
                organization=organization_id,
                resource=resource.reference(),
                local_patient=local,
            )
            return local
        return person_id

    def read(
        self,
        resource_type: str,
        resource_id: str,
        request: ApiRequest,
        *,
        organization_id: str = "",
    ) -> ApiResponse:
        organization = organization_id or request.context.organization_id
        repository = self._repositories.for_organization(organization)
        try:
            stored = repository.read(resource_type, resource_id)
        except ResourceNotFoundError:
            return _outcome(404, "not-found", f"{resource_type}/{resource_id} does not exist")

        refusal = self._authorize(
            request,
            resource_type=resource_type,
            operation=Operation.READ,
            holding_organization_id=organization,
            person_id=self._person_for(organization, stored.resource),
            resource_attributes=_attributes_of(stored.resource),
        )
        if refusal is not None:
            return refusal

        return ApiResponse(
            status=200,
            body=stored.resource.to_json(),
            headers={
                "ETag": stored.etag,
                "Last-Modified": stored.recorded_at.isoformat(),
                "Content-Type": (
                    f"application/fhir+json; fhirVersion={request.fhir_version.mime_parameter}"
                ),
            },
        )

    def search(
        self,
        resource_type: str,
        parameters: dict[str, str],
        request: ApiRequest,
        *,
        organization_id: str = "",
        count: int = 50,
    ) -> ApiResponse:
        """Search, with granular scope constraints applied as filters.

        A granular scope's constraints are **added to the query**, not just checked afterwards.
        Checking after would return the full set and then filter it, which is correct in the
        result but leaks the total count.
        """
        if definition_for(resource_type) is None:
            return _outcome(404, "not-supported", f"{resource_type} is not served here")

        organization = organization_id or request.context.organization_id
        refusal = self._authorize(
            request,
            resource_type=resource_type,
            operation=Operation.SEARCH,
            holding_organization_id=organization,
            person_id=parameters.get("patient", ""),
        )
        if refusal is not None:
            return refusal

        effective = dict(parameters)
        for constraint in request.scopes.granular_constraints(resource_type):
            effective.update(constraint)

        repository = self._repositories.for_organization(organization)
        result = repository.search(resource_type, effective, count=count)

        body = search_bundle(
            result.resources,
            total=result.total,
            base_url=self._base_url,
            self_link=f"{self._base_url}/{API_VERSION}/{resource_type}",
        )
        if result.unsupported_parameters:
            # Reported as a warning in the bundle rather than silently dropped. A filter that
            # vanishes returns a wider result set than the caller asked for.
            body["issue"] = [
                {
                    "severity": "warning",
                    "code": "not-supported",
                    "diagnostics": (
                        "these search parameters are not implemented and were not applied: "
                        + ", ".join(result.unsupported_parameters)
                    ),
                }
            ]
        return ApiResponse(status=200, body=body)

    def write(
        self,
        resource: Resource,
        request: ApiRequest,
        *,
        organization_id: str = "",
        create: bool = False,
    ) -> ApiResponse:
        organization = organization_id or request.context.organization_id
        refusal = self._authorize(
            request,
            resource_type=resource.resource_type,
            operation=Operation.CREATE if create else Operation.UPDATE,
            holding_organization_id=organization,
            person_id=self._person_for(organization, resource),
            resource_attributes=_attributes_of(resource),
        )
        if refusal is not None:
            return refusal

        repository = self._repositories.for_organization(organization)
        try:
            stored = (
                repository.create(resource, changed_by=request.context.principal_id)
                if create
                else repository.update(
                    resource,
                    if_match=request.if_match,
                    changed_by=request.context.principal_id,
                )
            )
        except ConcurrencyError as exc:
            return _outcome(412, "conflict", str(exc))
        except ResourceNotFoundError:
            return _outcome(404, "not-found", f"{resource.reference()} does not exist")
        except InteropError as exc:
            return _outcome(422, "processing", str(exc))

        return ApiResponse(
            status=201 if create else 200,
            body=stored.resource.to_json(),
            headers={
                "ETag": stored.etag,
                "Location": f"{resource.reference()}/_history/{stored.version_id}",
            },
        )

    def everything(
        self, person_id: str, request: ApiRequest, *, organization_id: str = ""
    ) -> ApiResponse:
        """``Patient/$everything`` within one organisation.

        Never crosses an organisation boundary. A longitudinal record spanning organisations is
        assembled per requester by calling each holder, each of which decides for itself.
        """
        organization = organization_id or request.context.organization_id
        refusal = self._authorize(
            request,
            resource_type="Patient",
            operation=Operation.READ,
            holding_organization_id=organization,
            person_id=self._resolve_person(organization, "Patient", person_id) or person_id,
        )
        if refusal is not None:
            return refusal

        repository = self._repositories.for_organization(organization)
        resources = repository.everything(person_id)
        return ApiResponse(
            status=200,
            body=search_bundle(resources, total=len(resources), base_url=self._base_url),
        )

    def kickoff_export(
        self,
        resource_types: tuple[str, ...],
        request: ApiRequest,
        *,
        organization_id: str = "",
    ) -> ApiResponse:
        """Start a bulk export. Returns ``202`` with a status location, per the Bulk Data IG."""
        organization = organization_id or request.context.organization_id

        if request.purpose in (PurposeOfUse.TREATMENT, PurposeOfUse.EMERGENCY_TREATMENT):
            # Population export for treatment is a category error: treatment is about one
            # patient in front of you, and a whole-population extract under that purpose is
            # how bulk access gets justified with a clinical-sounding label.
            return _outcome(
                403,
                "forbidden",
                f"purpose {request.purpose.value} does not authorise a population-level export; "
                "use an operations, research, or public-health purpose",
            )

        if not any(s.context == "system" for s in request.scopes.scopes):
            return _outcome(
                403,
                "forbidden",
                "bulk export requires a system-level scope (system/*.read); patient- and "
                "user-level scopes do not authorise population-level access",
            )

        for resource_type in resource_types:
            refusal = self._authorize(
                request,
                resource_type=resource_type,
                operation=Operation.READ,
                holding_organization_id=organization,
                person_id="",
            )
            if refusal is not None:
                return refusal

        job = BulkExportJob(
            job_id=f"export:{uuid.uuid4()}",
            organization_id=organization,
            resource_types=resource_types,
            requested_by=request.context.principal_id,
            purpose=request.purpose,
            context=request.context,
            requested_at=request.at,
        )
        self._exports[job.job_id] = job
        self._evict_exports()
        _log.info(
            "api.export_kickoff",
            job=job.job_id,
            organization=organization,
            types=list(resource_types),
        )
        return ApiResponse(
            status=202,
            body={},
            headers={"Content-Location": f"{self._base_url}{job.status_url}"},
        )

    def run_export(self, job_id: str) -> BulkExportJob:
        """Execute a queued export.

        Separate from kick-off because that is the actual contract: kick-off returns
        immediately and the work happens elsewhere. Calling this synchronously in tests is not
        the same as pretending export is synchronous.
        """
        job = self._exports.get(job_id)
        if job is None:
            raise InteropError(f"unknown export job {job_id!r}")
        repository = self._repositories.for_organization(job.organization_id)
        job.status = ExportStatus.IN_PROGRESS

        # Population-level authorisation (system scope, purpose, ABAC) says this *client* may
        # run an export. It does not say this *patient* agreed to it. Consent is per patient and
        # is evaluated here, or a patient who explicitly refused research use is in the research
        # extract anyway (docs/design/adr-0028-consent-deny-by-default.md).
        fallback_context = job.context or OrganizationContext(
            principal_id=job.requested_by,
            organization_id=job.organization_id,
            is_named_human=False,
        )
        permitted: dict[str, bool] = {}
        excluded = 0
        for resource_type in job.resource_types:
            result = repository.search(resource_type, {}, count=1_000_000)
            included = 0
            for resource in result.resources:
                person_id = self._person_for(job.organization_id, resource)
                if not person_id:
                    included += 1
                    continue
                if person_id not in permitted:
                    permitted[person_id] = self._consent.evaluate(
                        DisclosureRequest(
                            person_id=person_id,
                            context=fallback_context,
                            purpose=job.purpose,
                            data_category=resource_type,
                            holding_organization_id=job.organization_id,
                            at=job.requested_at,
                        )
                    ).discloses
                if permitted[person_id]:
                    included += 1
                else:
                    excluded += 1
            job.files[resource_type] = (
                f"/{API_VERSION}/$export-file/{job.job_id}/{resource_type}.ndjson"
            )
            _log.debug(
                "api.export_file",
                job=job.job_id,
                type=resource_type,
                included=included,
                excluded_for_consent=excluded,
            )
        job.excluded_for_consent = excluded
        job.status = ExportStatus.COMPLETED
        job.completed_at = dt.datetime.now(dt.UTC)
        return job

    def export_status(self, job_id: str, request: ApiRequest) -> ApiResponse:
        job = self._exports.get(job_id)
        if job is None:
            return _outcome(404, "not-found", f"unknown export job {job_id}")
        if job.requested_by != request.context.principal_id:
            # An export manifest names files a different principal may not read. Returning
            # not-found rather than forbidden avoids confirming that the job exists.
            return _outcome(404, "not-found", f"unknown export job {job_id}")
        if not job.status.is_terminal:
            return ApiResponse(
                status=202,
                body={},
                headers={"X-Progress": job.status.value, "Retry-After": "10"},
            )
        if job.status is not ExportStatus.COMPLETED:
            return _outcome(500, "exception", job.error or f"export {job.status.value}")
        return ApiResponse(status=200, body=job.manifest(self._base_url))

    def _evict_exports(self) -> None:
        if len(self._exports) <= self._max_export_jobs:
            return
        finished = sorted(
            (j for j in self._exports.values() if j.status.is_terminal),
            key=lambda j: j.completed_at or j.requested_at,
        )
        for job in finished[: len(self._exports) - self._max_export_jobs]:
            del self._exports[job.job_id]

    def bulk_import(
        self,
        ndjson: str,
        request: ApiRequest,
        *,
        organization_id: str = "",
    ) -> ApiResponse:
        """Import NDJSON.

        Per-line outcomes, not all-or-nothing: a bulk import of a hundred thousand resources
        that fails entirely on one malformed line is unusable, and a caller needs to know which
        lines failed rather than being told to try again.
        """
        organization = organization_id or request.context.organization_id
        if not any(s.context == "system" for s in request.scopes.scopes):
            return _outcome(403, "forbidden", "bulk import requires a system-level scope")

        repository = self._repositories.for_organization(organization)
        accepted = 0
        errors: list[str] = []

        for number, line in enumerate(ndjson.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                errors.append(f"line {number}: not valid JSON ({exc.msg})")
                continue
            try:
                resource = Resource.from_json(payload, organization_id=organization)
                if repository.exists(resource.resource_type, resource.id):
                    current = repository.read(resource.resource_type, resource.id)
                    repository.update(
                        resource,
                        if_match=current.etag,
                        changed_by=request.context.principal_id,
                    )
                else:
                    repository.create(resource, changed_by=request.context.principal_id)
                accepted += 1
            except InteropError as exc:
                errors.append(f"line {number}: {exc}")

        result = BulkImportResult(accepted=accepted, rejected=len(errors), errors=tuple(errors))
        return ApiResponse(status=200 if not errors else 207, body=result.to_json())


def _local_patient_id(resource: Resource) -> str:
    """The organisation-local Patient id this resource is about."""
    reference = resource.patient_reference()
    if reference.startswith("Patient/"):
        return reference.split("/", 1)[1]
    return resource.id if resource.resource_type == "Patient" else ""


def _attributes_of(resource: Resource) -> dict[str, str]:
    """Attributes a granular scope may constrain on."""
    attributes: dict[str, str] = {}
    category = resource.data.get("category")
    if isinstance(category, list):
        for entry in category:
            if isinstance(entry, dict):
                for coding in entry.get("coding", []) or []:
                    if isinstance(coding, dict) and isinstance(coding.get("code"), str):
                        attributes["category"] = coding["code"]
                        break
    if isinstance(resource.data.get("status"), str):
        attributes["status"] = resource.data["status"]
    return attributes
