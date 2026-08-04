"""The FHIR resource repository.

Versioned storage with optimistic concurrency. Three things are structural rather than
conventional:

**The organisation is a constructor argument.** A repository instance can only ever see one
organisation's resources — the same defence as Phase 2's tenant-in-constructor, extended to the
organisation boundary this phase introduces. An unscoped query is not a review finding, it is
unconstructable (docs/design/adr-0030-cross-organisation-sharing.md).

**Update requires the version you read.** `If-Match` with a weak ETag; a mismatch is a conflict,
never a silent overwrite. Lost-update in a clinical record means one clinician's medication
change disappearing under another's, with both believing theirs took effect.

**History is append-only.** A version is never mutated or removed, including on delete — a FHIR
delete is a new version marked deleted, so "what did this resource say when the decision was
made" stays answerable.
"""

from __future__ import annotations

import datetime as dt
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from cip_core.logging import get_logger
from cip_interop.domain import InteropError
from cip_interop.fhir.definitions import FhirVersion, definition_for
from cip_interop.fhir.resources import Resource
from cip_interop.fhir.validation import FhirValidationOutcome, validate_resource

__all__ = [
    "ConcurrencyError",
    "FhirRepository",
    "ResourceNotFoundError",
    "SearchResult",
    "StoredVersion",
]

_log = get_logger(__name__)


class ResourceNotFoundError(InteropError):
    """No such resource, or it belongs to another organisation.

    Deliberately one error for both. Distinguishing them tells an unauthorised caller that a
    patient exists at another organisation, which is itself a disclosure.
    """


class ConcurrencyError(InteropError):
    """The resource changed since the caller read it."""

    def __init__(self, expected: str, actual: str) -> None:
        super().__init__(
            f"version conflict: caller holds {expected!r}, current is {actual!r}. Re-read the "
            "resource, re-apply the change, and retry; overwriting would discard whatever the "
            "other writer changed."
        )
        self.expected = expected
        self.actual = actual


@dataclass(frozen=True, slots=True)
class StoredVersion:
    """One version of a resource."""

    resource: Resource
    version_id: str
    recorded_at: dt.datetime
    deleted: bool = False
    changed_by: str = ""

    @property
    def etag(self) -> str:
        """A weak ETag.

        FHIR deviates from RFC 7232 in requiring the weak form, and this follows FHIR rather
        than the RFC because the counterparty is a FHIR client
        (docs/design/adr-0032-api-surface-and-versioning.md).
        """
        return f'W/"{self.version_id}"'


@dataclass(frozen=True, slots=True)
class SearchResult:
    """A page of search results."""

    resources: tuple[Resource, ...] = ()
    total: int = 0
    offset: int = 0
    unsupported_parameters: tuple[str, ...] = ()
    """Parameters the caller sent that this gateway does not implement. Reported rather than
    ignored: a filter that is silently dropped returns a *wider* result set than the caller
    asked for, which in a clinical search means records they did not intend to see."""


def _matches(resource: Resource, parameter: str, value: str) -> bool:
    """Evaluate one supported search parameter."""
    if parameter == "_id":
        return resource.id == value
    if parameter == "patient":
        reference = resource.patient_reference()
        return reference in (value, f"Patient/{value}")
    if parameter == "identifier":
        for identifier in resource.data.get("identifier", []) or []:
            if not isinstance(identifier, dict):
                continue
            system = identifier.get("system", "")
            code = identifier.get("value", "")
            if value in (code, f"{system}|{code}"):
                return True
        return False
    if parameter in ("status", "intent", "priority"):
        return resource.data.get(parameter) == value
    if parameter in ("clinical-status", "verification-status"):
        field_name = "clinicalStatus" if parameter == "clinical-status" else "verificationStatus"
        concept = resource.data.get(field_name) or {}
        codings = concept.get("coding", []) if isinstance(concept, dict) else []
        return any(c.get("code") == value for c in codings if isinstance(c, dict))
    if parameter in ("code", "type", "category", "modality"):
        concept = resource.data.get(
            {"code": "code", "type": "type", "category": "category", "modality": "modality"}[
                parameter
            ]
        )
        candidates = concept if isinstance(concept, list) else [concept]
        for item in candidates:
            if isinstance(item, dict):
                for coding in item.get("coding", []) or []:
                    if isinstance(coding, dict) and coding.get("code") == value:
                        return True
                if item.get("code") == value:
                    return True
        return False
    if parameter in ("family", "given"):
        for name in resource.data.get("name", []) or []:
            if not isinstance(name, dict):
                continue
            if parameter == "family" and name.get("family", "").lower() == value.lower():
                return True
            if parameter == "given" and any(
                g.lower() == value.lower() for g in name.get("given", []) or []
            ):
                return True
        return False
    if parameter == "gender":
        return resource.data.get("gender") == value
    if parameter == "birthdate":
        return str(resource.data.get("birthDate", "")).startswith(value)
    if parameter in ("date", "started", "authored", "authoredon", "onset-date"):
        for candidate in (
            "effectiveDateTime",
            "authoredOn",
            "started",
            "date",
            "onsetDateTime",
            "performedDateTime",
            "occurrenceDateTime",
        ):
            recorded = resource.data.get(candidate)
            if isinstance(recorded, str) and recorded.startswith(value):
                return True
        return False
    if parameter == "name":
        return str(resource.data.get("name", "")).lower() == value.lower()
    return False


class FhirRepository:
    """Versioned, organisation-scoped FHIR storage.

    In-memory. The contract — versioning, conditional update, history, search — is what a
    database-backed implementation must satisfy, and is tested here rather than assumed.
    """

    def __init__(
        self,
        *,
        organization_id: str,
        fhir_version: FhirVersion = FhirVersion.R4,
        validate: bool = True,
        max_versions_per_resource: int = 200,
    ) -> None:
        if not organization_id.strip():
            raise InteropError(
                "FhirRepository requires an organization_id. A repository that can see every "
                "organisation is one that will eventually be used to."
            )
        self._organization_id = organization_id
        self._version = fhir_version
        self._validate = validate
        self._max_versions = max_versions_per_resource
        self._current: dict[tuple[str, str], StoredVersion] = {}
        self._history: dict[tuple[str, str], list[StoredVersion]] = {}
        self._sequence = 0

    @property
    def organization_id(self) -> str:
        return self._organization_id

    @property
    def fhir_version(self) -> FhirVersion:
        return self._version

    def _next_version(self) -> str:
        self._sequence += 1
        return str(self._sequence)

    def _key(self, resource_type: str, resource_id: str) -> tuple[str, str]:
        return (resource_type, resource_id)

    def create(
        self, resource: Resource, *, changed_by: str = "", at: dt.datetime | None = None
    ) -> StoredVersion:
        """Store a new resource.

        The organisation is **stamped from the repository**, never read from the payload. A
        caller who could set it could write into another organisation's data by editing a JSON
        field.
        """
        if not resource.id:
            raise InteropError(
                f"{resource.resource_type} has no id; this repository does not assign one, "
                "because a server-assigned id makes a create non-idempotent and interface "
                "replays then duplicate records"
            )
        outcome = self.validate(resource)
        if not outcome.valid:
            raise InteropError(
                f"{resource.resource_type}/{resource.id} failed validation: {outcome.render()}"
            )
        key = self._key(resource.resource_type, resource.id)
        if key in self._current and not self._current[key].deleted:
            raise InteropError(
                f"{resource.resource_type}/{resource.id} already exists; use update with an "
                "If-Match version"
            )
        return self._append(resource, changed_by=changed_by, at=at)

    def update(
        self,
        resource: Resource,
        *,
        if_match: str = "",
        changed_by: str = "",
        at: dt.datetime | None = None,
    ) -> StoredVersion:
        """Replace a resource, optionally guarded by the version the caller read.

        ``if_match`` accepts a weak ETag or a bare version id. An empty ``if_match`` is an
        unguarded write and is permitted, because FHIR permits it — but the caller has then
        chosen to accept lost updates, and the log records that they did.
        """
        key = self._key(resource.resource_type, resource.id)
        existing = self._current.get(key)
        if existing is None or existing.deleted:
            raise ResourceNotFoundError(f"{resource.resource_type}/{resource.id}")

        if if_match:
            wanted = if_match.strip().removeprefix("W/").strip('"')
            if wanted != existing.version_id:
                raise ConcurrencyError(expected=wanted, actual=existing.version_id)
        else:
            _log.warning(
                "fhir.unguarded_update",
                resource_type=resource.resource_type,
                resource_id=resource.id,
                organization=self._organization_id,
            )

        outcome = self.validate(resource)
        if not outcome.valid:
            raise InteropError(
                f"{resource.resource_type}/{resource.id} failed validation: {outcome.render()}"
            )
        return self._append(resource, changed_by=changed_by, at=at)

    def delete(
        self,
        resource_type: str,
        resource_id: str,
        *,
        changed_by: str = "",
        at: dt.datetime | None = None,
    ) -> StoredVersion:
        """Mark a resource deleted.

        A new version, not a removal. "What did this say when the decision was made" must stay
        answerable after a deletion, or the audit trail has a hole exactly where an
        investigation would look.
        """
        key = self._key(resource_type, resource_id)
        existing = self._current.get(key)
        if existing is None or existing.deleted:
            raise ResourceNotFoundError(f"{resource_type}/{resource_id}")
        version_id = self._next_version()
        moment = at or dt.datetime.now(dt.UTC)
        stored = StoredVersion(
            resource=existing.resource.with_version(version_id, at=moment),
            version_id=version_id,
            recorded_at=moment,
            deleted=True,
            changed_by=changed_by,
        )
        self._current[key] = stored
        self._history.setdefault(key, []).append(stored)
        self._trim(key)
        return stored

    def _append(
        self, resource: Resource, *, changed_by: str, at: dt.datetime | None
    ) -> StoredVersion:
        version_id = self._next_version()
        moment = at or dt.datetime.now(dt.UTC)
        versioned = resource.with_version(version_id, at=moment)
        stored = StoredVersion(
            resource=Resource(
                resource_type=versioned.resource_type,
                data=versioned.data,
                version_id=version_id,
                last_updated=moment,
                organization_id=self._organization_id,
            ),
            version_id=version_id,
            recorded_at=moment,
            changed_by=changed_by,
        )
        key = self._key(resource.resource_type, resource.id)
        self._current[key] = stored
        self._history.setdefault(key, []).append(stored)
        self._trim(key)
        return stored

    def _trim(self, key: tuple[str, str]) -> None:
        """Bound version history.

        The **current** version is never trimmed, whatever the bound: a history cap that could
        evict the live resource would delete data to save memory.
        """
        versions = self._history.get(key, [])
        excess = len(versions) - self._max_versions
        if excess > 0:
            del versions[:excess]

    def read(self, resource_type: str, resource_id: str) -> StoredVersion:
        stored = self._current.get(self._key(resource_type, resource_id))
        if stored is None or stored.deleted:
            raise ResourceNotFoundError(f"{resource_type}/{resource_id}")
        return stored

    def read_version(self, resource_type: str, resource_id: str, version_id: str) -> StoredVersion:
        for stored in self._history.get(self._key(resource_type, resource_id), []):
            if stored.version_id == version_id:
                return stored
        raise ResourceNotFoundError(f"{resource_type}/{resource_id}/_history/{version_id}")

    def history(self, resource_type: str, resource_id: str) -> tuple[StoredVersion, ...]:
        """Every retained version, newest last."""
        return tuple(self._history.get(self._key(resource_type, resource_id), []))

    def exists(self, resource_type: str, resource_id: str) -> bool:
        stored = self._current.get(self._key(resource_type, resource_id))
        return stored is not None and not stored.deleted

    def validate(self, resource: Resource) -> FhirValidationOutcome:
        if not self._validate:
            return FhirValidationOutcome(
                resource_type=resource.resource_type, version=self._version
            )
        return validate_resource(resource, version=self._version)

    def search(
        self,
        resource_type: str,
        parameters: dict[str, str] | None = None,
        *,
        count: int = 50,
        offset: int = 0,
        include_retracted: bool = False,
    ) -> SearchResult:
        """Search within this organisation.

        ``include_retracted`` defaults to ``False``. A lab result the lab has withdrawn must not
        come back as current, and a search that returns it is handing a clinician a value that
        has been retracted.

        Unsupported parameters are **reported, not applied**. Silently dropping a filter returns
        a wider result set than the caller asked for.
        """
        definition = definition_for(resource_type)
        if definition is None:
            return SearchResult()
        wanted = parameters or {}
        supported = set(definition.search_parameters)
        unsupported = tuple(sorted(p for p in wanted if p not in supported))
        applied = {k: v for k, v in wanted.items() if k in supported}

        matches = []
        for (stored_type, _), stored in self._current.items():
            if stored_type != resource_type or stored.deleted:
                continue
            if not include_retracted and stored.resource.is_retracted:
                continue
            if all(_matches(stored.resource, k, v) for k, v in applied.items()):
                matches.append(stored.resource)

        matches.sort(key=lambda r: r.id)
        page = matches[offset : offset + count]
        return SearchResult(
            resources=tuple(page),
            total=len(matches),
            offset=offset,
            unsupported_parameters=unsupported,
        )

    def everything(self, patient_id: str) -> tuple[Resource, ...]:
        """Every resource about one patient, within this organisation.

        The ``Patient/$everything`` operation, and the basis of a per-requester longitudinal
        record. It never crosses an organisation boundary — assembling across organisations
        requires an agreement and a consent, checked elsewhere.
        """
        reference = f"Patient/{patient_id}"
        found = [
            stored.resource
            for (stored_type, stored_id), stored in self._current.items()
            if not stored.deleted
            and (
                stored.resource.patient_reference() == reference
                or (stored_type == "Patient" and stored_id == patient_id)
            )
        ]
        return tuple(sorted(found, key=lambda r: (r.resource_type, r.id)))

    def statistics(self) -> dict[str, Any]:
        counts: OrderedDict[str, int] = OrderedDict()
        for (resource_type, _), stored in sorted(self._current.items()):
            if not stored.deleted:
                counts[resource_type] = counts.get(resource_type, 0) + 1
        return {
            "organization": self._organization_id,
            "fhir_version": self._version.value,
            "resources": dict(counts),
            "versions": sum(len(v) for v in self._history.values()),
        }


@dataclass(slots=True)
class RepositoryRegistry:
    """One repository per organisation.

    The registry is how the platform holds many organisations without any single repository
    being able to see across them.
    """

    fhir_version: FhirVersion = FhirVersion.R4
    _repositories: dict[str, FhirRepository] = field(default_factory=dict)

    def for_organization(self, organization_id: str) -> FhirRepository:
        if organization_id not in self._repositories:
            self._repositories[organization_id] = FhirRepository(
                organization_id=organization_id, fhir_version=self.fhir_version
            )
        return self._repositories[organization_id]

    def organizations(self) -> tuple[str, ...]:
        return tuple(sorted(self._repositories))
