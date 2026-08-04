"""Data lake export: layered extracts, de-identification, and a feature store.

Bronze, silver, gold — raw as received, normalised to the canonical model, and curated for a
consumer. The layering matters because reprocessing is normal: a mapping bug found six months
in is fixed by replaying bronze, and a lake that only kept the transformed output cannot.

**Safe Harbor is implemented; Expert Determination is not claimed**
(docs/design/adr-0031-deidentification-safe-harbor.md). Safe Harbor is a mechanical rule and so
is implementable. Expert Determination is a qualified person's opinion about a specific dataset
released into a specific context, and no code can produce one. A ``DeidentificationMethod`` with
no expert-determination member is the honest API.

Every extract carries a manifest naming the method, the ruleset version, and what was acted on,
so a released dataset can be traced back to exactly the rules that produced it.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from cip_core.logging import get_logger
from cip_interop.domain import InteropError
from cip_interop.fhir.resources import Resource

__all__ = [
    "SAFE_HARBOR_CATEGORIES",
    "DeidentificationMethod",
    "DeidentificationResult",
    "ExportManifest",
    "FeatureStore",
    "LakeLayer",
    "deidentify",
]

_log = get_logger(__name__)

#: The 18 HIPAA Safe Harbor identifier categories. Enumerated so the manifest can name what was
#: acted on rather than asserting "de-identified" and leaving a recipient to trust it.
SAFE_HARBOR_CATEGORIES: tuple[str, ...] = (
    "names",
    "geographic_subdivisions_smaller_than_state",
    "dates_other_than_year",
    "telephone_numbers",
    "fax_numbers",
    "email_addresses",
    "social_security_numbers",
    "medical_record_numbers",
    "health_plan_beneficiary_numbers",
    "account_numbers",
    "certificate_license_numbers",
    "vehicle_identifiers",
    "device_identifiers",
    "web_urls",
    "ip_addresses",
    "biometric_identifiers",
    "full_face_photographs",
    "other_unique_identifying_numbers",
)

#: ZIP prefixes whose three-digit area holds 20,000 people or fewer. Safe Harbor requires these
#: to be suppressed entirely rather than truncated — truncation is not enough when the truncated
#: unit is already small.
RESTRICTED_ZIP_PREFIXES: frozenset[str] = frozenset(
    {
        "036",
        "059",
        "063",
        "102",
        "203",
        "556",
        "692",
        "790",
        "821",
        "823",
        "830",
        "831",
        "878",
        "879",
        "884",
        "890",
        "893",
    }
)

_DATE_LIKE = re.compile(r"^(\d{4})-\d{2}(-\d{2})?(T.*)?$")


class LakeLayer(StrEnum):
    """Where an extract sits."""

    BRONZE = "bronze"
    """Exactly as received, including the raw HL7. The layer that makes reprocessing possible."""
    SILVER = "silver"
    """Normalised to the canonical FHIR model, identity resolved."""
    GOLD = "gold"
    """Curated for a consumer: a cohort, a measure, a feature table."""

    @property
    def contains_phi(self) -> bool:
        """Bronze and silver always do. Gold does unless it has been de-identified — which is a
        property of the extract, not of the layer, so gold is not assumed safe."""
        return self in (LakeLayer.BRONZE, LakeLayer.SILVER)


class DeidentificationMethod(StrEnum):
    """How an extract was de-identified.

    There is deliberately **no expert determination member**. It is a statistical opinion by a
    qualified person about a specific dataset and release context; software cannot produce one,
    and an API offering it would let a pipeline assert something no code can assert.
    """

    NONE = "none"
    SAFE_HARBOR = "safe_harbor"
    LIMITED_DATA_SET = "limited_data_set"
    """Dates and geography retained, direct identifiers removed. A real HIPAA category that
    **requires a data use agreement** — labelled as such rather than presented as
    de-identified."""

    @property
    def requires_data_use_agreement(self) -> bool:
        return self is DeidentificationMethod.LIMITED_DATA_SET

    @property
    def is_deidentified(self) -> bool:
        return self is DeidentificationMethod.SAFE_HARBOR


@dataclass(frozen=True, slots=True)
class DeidentificationResult:
    """One de-identified resource and what was done to it."""

    resource: Resource
    removed: tuple[str, ...] = ()
    generalised: tuple[str, ...] = ()
    method: DeidentificationMethod = DeidentificationMethod.SAFE_HARBOR

    def render(self) -> str:
        return (
            f"{self.resource.resource_type}/{self.resource.id}: removed {len(self.removed)}, "
            f"generalised {len(self.generalised)}"
        )


def _pseudonym(value: str, salt: str) -> str:
    """A stable pseudonym.

    Keyed with a salt the recipient does not hold, so the same patient maps to the same
    pseudonym across an extract (which research needs) without the mapping being reversible by
    anyone holding the output (which Safe Harbor needs). **The salt must not travel with the
    data** — the manifest records that a salt was used, never the salt.
    """
    return hashlib.blake2b(f"{salt}|{value}".encode(), digest_size=16).hexdigest()


def _generalise_zip(postal: str) -> tuple[str, bool]:
    digits = re.sub(r"[^0-9]", "", postal)
    if len(digits) < 3:
        return "", True
    prefix = digits[:3]
    if prefix in RESTRICTED_ZIP_PREFIXES:
        # Safe Harbor: a three-digit area with 20,000 people or fewer is suppressed entirely,
        # because truncation does not de-identify a unit that is already small.
        return "", True
    return prefix, True


def _year_only(value: str) -> str | None:
    match = _DATE_LIKE.match(value)
    return match.group(1) if match else None


def deidentify(
    resource: Resource,
    *,
    method: DeidentificationMethod = DeidentificationMethod.SAFE_HARBOR,
    salt: str = "",
    age_years: int | None = None,
) -> DeidentificationResult:
    """Apply Safe Harbor, or produce a limited data set.

    ``age_years`` over 89 is aggregated. Safe Harbor requires it because the very old are
    identifiable by age alone in a small population, and it is the rule most often missed.
    """
    if method is DeidentificationMethod.NONE:
        return DeidentificationResult(resource=resource, method=method)
    if not salt:
        raise InteropError(
            "de-identification requires a salt so pseudonyms are stable within an extract "
            "without being reversible by its recipient"
        )

    data = dict(resource.data)
    removed: list[str] = []
    generalised: list[str] = []
    keep_dates = method is DeidentificationMethod.LIMITED_DATA_SET

    if "id" in data:
        data["id"] = _pseudonym(f"{resource.resource_type}/{data['id']}", salt)
        generalised.append("id")

    for element in ("name", "telecom", "photo", "contact", "communication"):
        if element in data:
            del data[element]
            removed.append(element)

    if "identifier" in data:
        del data["identifier"]
        removed.append("identifier")

    if "address" in data and isinstance(data["address"], list):
        rewritten = []
        for address in data["address"]:
            if not isinstance(address, dict):
                continue
            entry: dict[str, Any] = {}
            if method is DeidentificationMethod.LIMITED_DATA_SET:
                entry = {
                    k: v
                    for k, v in address.items()
                    if k in ("city", "state", "postalCode", "country")
                }
            else:
                if address.get("state"):
                    entry["state"] = address["state"]
                if address.get("postalCode"):
                    prefix, changed = _generalise_zip(str(address["postalCode"]))
                    if prefix:
                        entry["postalCode"] = prefix
                    if changed:
                        generalised.append("address.postalCode")
            rewritten.append(entry)
        data["address"] = [a for a in rewritten if a]
        removed.append("address.line")

    if not keep_dates:
        for element in list(data):
            value = data[element]
            if isinstance(value, str):
                year = _year_only(value)
                if year is not None:
                    data[element] = year
                    generalised.append(element)
        if "birthDate" in data and age_years is not None and age_years > 89:
            # Safe Harbor aggregates ages over 89. The birth year is removed too — keeping it
            # while reporting "90+" would make the aggregation pointless.
            del data["birthDate"]
            data.setdefault("extension", []).append(
                {"url": "urn:cip:age-band", "valueString": "90+"}
            )
            removed.append("birthDate")
            generalised.append("age")

    for reference_element in ("subject", "patient", "encounter", "requester", "performer"):
        value = data.get(reference_element)
        if isinstance(value, dict) and isinstance(value.get("reference"), str):
            resource_type = value["reference"].partition("/")[0]
            data[reference_element] = {
                "reference": f"{resource_type}/{_pseudonym(value['reference'], salt)}"
            }
            generalised.append(reference_element)
            if "display" in value:
                removed.append(f"{reference_element}.display")

    data["meta"] = {
        "security": [
            {
                "system": "http://terminology.hl7.org/CodeSystem/v3-ActCode",
                "code": "ANONYED" if method is DeidentificationMethod.SAFE_HARBOR else "RESTRICTED",
            }
        ]
    }

    return DeidentificationResult(
        resource=Resource(resource_type=resource.resource_type, data=data, organization_id=""),
        removed=tuple(dict.fromkeys(removed)),
        generalised=tuple(dict.fromkeys(generalised)),
        method=method,
    )


@dataclass(frozen=True, slots=True)
class ExportManifest:
    """What an extract contains and how it was produced.

    Travels with the data. A dataset whose de-identification method is not recorded on it will
    be treated as whatever the person who finds it assumes, and the safe assumption is not the
    one people make.
    """

    export_id: str
    layer: LakeLayer
    method: DeidentificationMethod
    ruleset_version: str
    resource_counts: dict[str, int]
    organization_id: str
    created_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))
    categories_acted_on: tuple[str, ...] = ()
    salted: bool = False
    note: str = ""

    @property
    def requires_data_use_agreement(self) -> bool:
        return self.method.requires_data_use_agreement

    def to_json(self) -> dict[str, Any]:
        return {
            "export_id": self.export_id,
            "layer": str(self.layer),
            "deidentification": {
                "method": str(self.method),
                "ruleset_version": self.ruleset_version,
                "categories": list(self.categories_acted_on),
                "salted": self.salted,
                "expert_determination": False,
                "residual_risk_note": (
                    "Safe Harbor removes the enumerated identifiers. It does not make "
                    "re-identification impossible: a rare diagnosis combined with a "
                    "three-digit ZIP and an age band can still single someone out."
                )
                if self.method.is_deidentified
                else "",
            },
            "requires_data_use_agreement": self.requires_data_use_agreement,
            "organization": self.organization_id,
            "resources": self.resource_counts,
            "created_at": self.created_at.isoformat(),
            "note": self.note,
        }


def export_ndjson(resources: tuple[Resource, ...]) -> str:
    """Serialise resources as NDJSON, the FHIR bulk-export format."""
    import json

    return "\n".join(json.dumps(r.to_json(), sort_keys=True) for r in resources)


@dataclass(frozen=True, slots=True)
class FeatureDefinition:
    """One feature in the store."""

    name: str
    description: str
    dtype: str
    source: str
    version: str = "1.0.0"
    #: Whether this feature is derived from data after the prediction time it will be used at.
    #: Leakage is the defect that makes an offline model look excellent and a deployed one
    #: useless, and it is a property of the *definition*, so it is declared here.
    point_in_time_safe: bool = True


@dataclass(slots=True)
class FeatureStore:
    """Point-in-time feature storage for model training and serving.

    Every value carries the timestamp it became known. A store that returns "the latest value"
    for a training row dated last year leaks the future into the past, and the resulting model
    scores beautifully offline and fails in production.
    """

    definitions: dict[str, FeatureDefinition] = field(default_factory=dict)
    _values: dict[tuple[str, str], list[tuple[dt.datetime, Any]]] = field(default_factory=dict)

    def define(self, definition: FeatureDefinition) -> None:
        if definition.name in self.definitions:
            raise InteropError(f"feature {definition.name!r} is already defined")
        self.definitions[definition.name] = definition

    def write(self, entity_id: str, feature: str, value: Any, *, known_at: dt.datetime) -> None:
        if feature not in self.definitions:
            raise InteropError(
                f"feature {feature!r} is not defined; an undefined feature has no declared "
                "point-in-time safety, so it cannot be used for training"
            )
        series = self._values.setdefault((entity_id, feature), [])
        series.append((known_at, value))
        series.sort(key=lambda item: item[0])

    def read_at(self, entity_id: str, feature: str, *, as_of: dt.datetime) -> Any:
        """The value as it was known at ``as_of``, never later.

        This is the whole point of the store.
        """
        series = self._values.get((entity_id, feature), [])
        latest = None
        for known_at, value in series:
            if known_at <= as_of:
                latest = value
            else:
                break
        return latest

    def training_row(
        self, entity_id: str, features: tuple[str, ...], *, as_of: dt.datetime
    ) -> dict[str, Any]:
        return {f: self.read_at(entity_id, f, as_of=as_of) for f in features}

    def statistics(self) -> dict[str, Any]:
        return {
            "features": len(self.definitions),
            "entities": len({entity for entity, _ in self._values}),
            "values": sum(len(v) for v in self._values.values()),
            "leakage_risk_features": [
                name for name, d in self.definitions.items() if not d.point_in_time_safe
            ],
        }
