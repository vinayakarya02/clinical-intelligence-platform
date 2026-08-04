"""Bundles: transaction, batch, and search results.

The difference between the two write types is the whole point and is routinely got wrong:

**transaction is atomic.** Every entry succeeds or none do. A transaction that half-applied
would leave an Encounter referencing a Patient that was rolled back.

**batch is not.** Entries are independent; one failure does not affect the others. Used when a
sender wants throughput and can reconcile per-entry outcomes itself.

Implementing both as "loop and apply" — which is what happens when nobody reads the
specification — gives batch semantics under a transaction label, and the caller finds out when
half a patient's admission is in the repository.

The other thing this handles is **internal references**. A transaction may create a Patient and
an Observation that references it in the same bundle, using a ``urn:uuid:`` placeholder. Those
have to be resolved to real ids after assignment and before the referencing resource is stored,
or the Observation points at a URN nothing can dereference.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from cip_core.logging import get_logger
from cip_interop.domain import ValidationError
from cip_interop.fhir.repository import ConcurrencyError, FhirRepository, ResourceNotFoundError
from cip_interop.fhir.resources import Resource

__all__ = [
    "BundleEntry",
    "BundleType",
    "EntryOutcome",
    "TransactionResult",
    "process_bundle",
    "search_bundle",
]

_log = get_logger(__name__)

_URN_PREFIX = "urn:uuid:"


class BundleType(StrEnum):
    """The bundle types this gateway processes."""

    TRANSACTION = "transaction"
    BATCH = "batch"
    TRANSACTION_RESPONSE = "transaction-response"
    BATCH_RESPONSE = "batch-response"
    SEARCHSET = "searchset"
    COLLECTION = "collection"
    HISTORY = "history"

    @property
    def is_atomic(self) -> bool:
        return self is BundleType.TRANSACTION

    @property
    def response_type(self) -> BundleType:
        return (
            BundleType.TRANSACTION_RESPONSE
            if self is BundleType.TRANSACTION
            else BundleType.BATCH_RESPONSE
        )


@dataclass(frozen=True, slots=True)
class BundleEntry:
    """One entry of a write bundle."""

    method: str
    url: str
    resource: Resource | None = None
    full_url: str = ""
    if_match: str = ""
    if_none_exist: str = ""

    @property
    def placeholder(self) -> str:
        """The ``urn:uuid:`` this entry will be known by inside the bundle, if any."""
        return self.full_url if self.full_url.startswith(_URN_PREFIX) else ""


@dataclass(frozen=True, slots=True)
class EntryOutcome:
    """What happened to one entry."""

    status: str
    location: str = ""
    etag: str = ""
    diagnostics: str = ""

    @property
    def succeeded(self) -> bool:
        return self.status.startswith(("200", "201", "204"))

    def to_json(self) -> dict[str, Any]:
        response: dict[str, Any] = {"status": self.status}
        if self.location:
            response["location"] = self.location
        if self.etag:
            response["etag"] = self.etag
        if self.diagnostics:
            response["outcome"] = {
                "resourceType": "OperationOutcome",
                "issue": [
                    {"severity": "error", "code": "processing", "diagnostics": self.diagnostics}
                ],
            }
        return {"response": response}


@dataclass(frozen=True, slots=True)
class TransactionResult:
    """The outcome of processing a whole bundle."""

    bundle_type: BundleType
    outcomes: tuple[EntryOutcome, ...] = ()
    applied: bool = True
    failure_reason: str = ""

    @property
    def succeeded(self) -> bool:
        return self.applied and all(o.succeeded for o in self.outcomes)

    def to_json(self) -> dict[str, Any]:
        return {
            "resourceType": "Bundle",
            "type": str(self.bundle_type.response_type),
            "entry": [o.to_json() for o in self.outcomes],
        }


def _parse_entry(raw: dict[str, Any], *, organization_id: str) -> BundleEntry:
    request = raw.get("request")
    if not isinstance(request, dict):
        raise ValidationError("bundle entry has no request; a write bundle requires one per entry")
    method = str(request.get("method", "")).upper()
    if method not in ("POST", "PUT", "DELETE"):
        raise ValidationError(
            f"unsupported bundle entry method {method!r}; POST, PUT, and DELETE are handled"
        )
    payload = raw.get("resource")
    resource = (
        Resource.from_json(payload, organization_id=organization_id)
        if isinstance(payload, dict)
        else None
    )
    if method in ("POST", "PUT") and resource is None:
        raise ValidationError(f"{method} bundle entry has no resource")
    return BundleEntry(
        method=method,
        url=str(request.get("url", "")),
        resource=resource,
        full_url=str(raw.get("fullUrl", "")),
        if_match=str(request.get("ifMatch", "")),
        if_none_exist=str(request.get("ifNoneExist", "")),
    )


def _substitute(value: Any, resolved: dict[str, str]) -> Any:
    """Replace ``urn:uuid:`` placeholders with real references, recursively."""
    if isinstance(value, dict):
        return {k: _substitute(v, resolved) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute(v, resolved) for v in value]
    if isinstance(value, str) and value.startswith(_URN_PREFIX):
        return resolved.get(value, value)
    return value


def _unresolved_placeholders(data: Any, resolved: dict[str, str]) -> list[str]:
    found: list[str] = []
    if isinstance(data, dict):
        for value in data.values():
            found.extend(_unresolved_placeholders(value, resolved))
    elif isinstance(data, list):
        for value in data:
            found.extend(_unresolved_placeholders(value, resolved))
    elif isinstance(data, str) and data.startswith(_URN_PREFIX) and data not in resolved:
        found.append(data)
    return found


def process_bundle(
    payload: dict[str, Any],
    repository: FhirRepository,
    *,
    changed_by: str = "",
    at: dt.datetime | None = None,
) -> TransactionResult:
    """Apply a transaction or batch bundle.

    A transaction is applied against a **staged copy** and committed only if every entry
    succeeded. Atomicity is the contract; simulating it by applying and then trying to undo
    would leave the repository briefly inconsistent and would not survive a crash between the
    two halves.
    """
    declared = payload.get("type")
    try:
        bundle_type = BundleType(str(declared))
    except ValueError as exc:
        raise ValidationError(
            f"bundle type {declared!r} is not one this gateway processes"
        ) from exc
    if bundle_type not in (BundleType.TRANSACTION, BundleType.BATCH):
        raise ValidationError(
            f"bundle type {bundle_type} is not writable; only transaction and batch are applied"
        )

    raw_entries = payload.get("entry")
    if not isinstance(raw_entries, list):
        raise ValidationError("bundle has no entry list")

    entries = [_parse_entry(e, organization_id=repository.organization_id) for e in raw_entries]

    # Placeholders are resolved before anything is written, so a resource can reference another
    # created in the same bundle. Ids are assigned here rather than by the repository, because
    # the reference has to exist before the target is stored.
    resolved: dict[str, str] = {}
    for entry in entries:
        if entry.placeholder and entry.resource is not None:
            assigned = entry.resource.id or str(uuid.uuid4())
            resolved[entry.placeholder] = f"{entry.resource.resource_type}/{assigned}"

    prepared: list[tuple[BundleEntry, Resource | None]] = []
    for entry in entries:
        if entry.resource is None:
            prepared.append((entry, None))
            continue
        data = dict(entry.resource.data)
        if entry.placeholder:
            data["id"] = resolved[entry.placeholder].split("/", 1)[1]
        substituted = _substitute(data, resolved)
        prepared.append((entry, entry.resource.with_data(substituted)))

    if bundle_type.is_atomic:
        for _entry, resource in prepared:
            if resource is None:
                continue
            dangling = _unresolved_placeholders(resource.data, resolved)
            if dangling:
                return TransactionResult(
                    bundle_type=bundle_type,
                    outcomes=(),
                    applied=False,
                    failure_reason=(
                        f"{resource.resource_type} references {dangling[0]!r}, which no entry in "
                        "this bundle creates; the transaction would store a dangling reference"
                    ),
                )

    outcomes: list[EntryOutcome] = []
    staged: list[tuple[BundleEntry, Resource | None]] = []

    for entry, resource in prepared:
        outcome = _dry_run(entry, resource, repository)
        outcomes.append(outcome)
        if outcome.succeeded:
            staged.append((entry, resource))
        elif bundle_type.is_atomic:
            return TransactionResult(
                bundle_type=bundle_type,
                outcomes=tuple(outcomes),
                applied=False,
                failure_reason=outcome.diagnostics,
            )

    committed: list[EntryOutcome] = []
    for entry, resource in staged:
        committed.append(_apply(entry, resource, repository, changed_by=changed_by, at=at))

    if bundle_type.is_atomic:
        _log.info(
            "fhir.transaction_committed",
            entries=len(committed),
            organization=repository.organization_id,
        )
        return TransactionResult(bundle_type=bundle_type, outcomes=tuple(committed), applied=True)

    # Batch: the failures kept their dry-run outcomes, the successes get their real ones.
    merged: list[EntryOutcome] = []
    applied_iter = iter(committed)
    for outcome in outcomes:
        merged.append(next(applied_iter) if outcome.succeeded else outcome)
    return TransactionResult(bundle_type=bundle_type, outcomes=tuple(merged), applied=True)


def _dry_run(
    entry: BundleEntry, resource: Resource | None, repository: FhirRepository
) -> EntryOutcome:
    """Decide whether an entry would succeed, without changing anything.

    Every reason an apply can fail must be detectable here, or transaction atomicity is a claim
    rather than a property.
    """
    if entry.method == "DELETE":
        parts = entry.url.split("/")
        if len(parts) < 2:
            return EntryOutcome("400", diagnostics=f"DELETE url {entry.url!r} is not Type/id")
        if not repository.exists(parts[0], parts[1]):
            return EntryOutcome("404", diagnostics=f"{entry.url} does not exist")
        return EntryOutcome("204")

    if resource is None:
        return EntryOutcome("400", diagnostics="entry has no resource")

    if not resource.id:
        return EntryOutcome(
            "400",
            diagnostics=(
                f"{resource.resource_type} has no id and no urn:uuid: fullUrl to assign one"
            ),
        )

    outcome = repository.validate(resource)
    if not outcome.valid:
        return EntryOutcome("422", diagnostics=outcome.errors[0].render())

    exists = repository.exists(resource.resource_type, resource.id)
    if entry.method == "POST" and exists:
        return EntryOutcome(
            "409", diagnostics=f"{resource.reference()} already exists; POST would duplicate it"
        )
    if entry.method == "PUT":
        if not exists:
            return EntryOutcome("404", diagnostics=f"{resource.reference()} does not exist")
        if entry.if_match:
            current = repository.read(resource.resource_type, resource.id)
            wanted = entry.if_match.strip().removeprefix("W/").strip('"')
            if wanted != current.version_id:
                return EntryOutcome(
                    "412",
                    diagnostics=(
                        f"version conflict on {resource.reference()}: caller holds {wanted!r}, "
                        f"current is {current.version_id!r}"
                    ),
                )
    return EntryOutcome("201" if entry.method == "POST" else "200")


def _apply(
    entry: BundleEntry,
    resource: Resource | None,
    repository: FhirRepository,
    *,
    changed_by: str,
    at: dt.datetime | None,
) -> EntryOutcome:
    try:
        if entry.method == "DELETE":
            parts = entry.url.split("/")
            stored = repository.delete(parts[0], parts[1], changed_by=changed_by, at=at)
            return EntryOutcome("204", location=entry.url, etag=stored.etag)
        assert resource is not None
        if entry.method == "POST":
            stored = repository.create(resource, changed_by=changed_by, at=at)
            status = "201"
        else:
            stored = repository.update(
                resource, if_match=entry.if_match, changed_by=changed_by, at=at
            )
            status = "200"
        return EntryOutcome(
            status,
            location=f"{resource.reference()}/_history/{stored.version_id}",
            etag=stored.etag,
        )
    except (ConcurrencyError, ResourceNotFoundError, ValidationError) as exc:
        # Reachable only if the repository changed between the dry run and the commit, which
        # single-threaded processing rules out and a concurrent one does not.
        return EntryOutcome("409", diagnostics=str(exc))


def search_bundle(
    resources: tuple[Resource, ...],
    *,
    total: int,
    base_url: str = "",
    self_link: str = "",
) -> dict[str, Any]:
    """Build a ``searchset`` bundle.

    ``total`` is the number of matches, not the number of entries in this page. Setting it to
    the page size is a common bug that makes a client believe it has everything.
    """
    return {
        "resourceType": "Bundle",
        "type": str(BundleType.SEARCHSET),
        "total": total,
        "link": [{"relation": "self", "url": self_link}] if self_link else [],
        "entry": [
            {
                "fullUrl": f"{base_url}/{r.reference()}" if base_url else r.reference(),
                "resource": r.to_json(),
                "search": {"mode": "match"},
            }
            for r in resources
        ],
    }


@dataclass(slots=True)
class BundleBuilder:
    """Assembles a transaction bundle.

    Used by the mapping layer, which turns one HL7 message into several resources that must be
    stored together — an ORU produces a DiagnosticReport and its Observations, and storing the
    report without its results is worse than storing neither.
    """

    bundle_type: BundleType = BundleType.TRANSACTION
    _entries: list[dict[str, Any]] = field(default_factory=list)

    def add(self, resource: Resource, *, method: str = "POST", if_match: str = "") -> BundleBuilder:
        request: dict[str, Any] = {
            "method": method,
            "url": resource.resource_type if method == "POST" else resource.reference(),
        }
        if if_match:
            request["ifMatch"] = if_match
        self._entries.append(
            {
                "fullUrl": f"{_URN_PREFIX}{uuid.uuid4()}"
                if not resource.id
                else resource.reference(),
                "resource": resource.to_json(),
                "request": request,
            }
        )
        return self

    def build(self) -> dict[str, Any]:
        return {
            "resourceType": "Bundle",
            "type": str(self.bundle_type),
            "entry": list(self._entries),
        }
