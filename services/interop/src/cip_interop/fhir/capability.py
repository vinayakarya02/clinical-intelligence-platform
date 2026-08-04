"""The CapabilityStatement, generated from what is actually registered.

Hand-written capability statements drift, and a client that trusts a capability the server does
not have fails at the worst time — often silently, by sending a search parameter the server
ignores and receiving a wider result set than it asked for.

This one is generated from the resource definitions, so it can only ever **understate**: a
search parameter absent from the definitions is absent from the statement, and no client is
told about a search that does not work.
"""

from __future__ import annotations

from typing import Any

from cip_interop.fhir.definitions import FhirVersion, definition_for, supported_resource_types

__all__ = ["capability_statement"]

#: Interactions implemented for every resource type. Uniform, so the statement does not need a
#: per-resource exception table that would be one more thing to keep true.
_INTERACTIONS = ("read", "vread", "update", "delete", "history-instance", "search-type", "create")


def capability_statement(
    *,
    version: FhirVersion = FhirVersion.R4,
    base_url: str = "",
    software_version: str = "6.0.0",
    organization: str = "",
) -> dict[str, Any]:
    """Build the statement for one FHIR version.

    Deliberately declares the things this gateway does **not** do, because the alternative is
    that a client discovers them by getting wrong answers:

    - no ``searchInclude`` / ``searchRevInclude`` — chained and reverse-chained search is not
      implemented, and a partial implementation would silently return incomplete result sets
    - no ``Subscription``
    - no terminology operations (``$expand``, ``$validate-code``)
    """
    resources = []
    for resource_type in supported_resource_types():
        definition = definition_for(resource_type)
        if definition is None:  # pragma: no cover - keys come from the same table
            continue
        resources.append(
            {
                "type": resource_type,
                "profile": f"http://hl7.org/fhir/StructureDefinition/{resource_type}",
                "interaction": [{"code": code} for code in _INTERACTIONS],
                "versioning": "versioned-update",
                "readHistory": True,
                "updateCreate": False,
                "conditionalCreate": False,
                "conditionalUpdate": False,
                "conditionalDelete": "not-supported",
                "searchParam": [
                    {"name": name, "type": _parameter_type(name)}
                    for name in definition.search_parameters
                ],
            }
        )

    return {
        "resourceType": "CapabilityStatement",
        "status": "active",
        "kind": "instance",
        "fhirVersion": version.value,
        "format": ["application/fhir+json"],
        "publisher": organization or "Clinical Intelligence Platform",
        "software": {"name": "CIP FHIR Gateway", "version": software_version},
        "implementation": {"description": "CIP interoperability gateway", "url": base_url},
        "rest": [
            {
                "mode": "server",
                "security": {
                    "service": [
                        {
                            "coding": [
                                {
                                    "system": "http://terminology.hl7.org/CodeSystem/restful-security-service",
                                    "code": "SMART-on-FHIR",
                                }
                            ]
                        }
                    ],
                    "extension": [
                        {
                            "url": "http://fhir-registry.smarthealthit.org/StructureDefinition/oauth-uris",
                            "extension": [
                                {"url": "token", "valueUri": f"{base_url}/oauth/token"},
                                {"url": "authorize", "valueUri": f"{base_url}/oauth/authorize"},
                            ],
                        }
                    ],
                },
                "resource": resources,
                "interaction": [{"code": "transaction"}, {"code": "batch"}],
                "operation": [
                    {
                        "name": "export",
                        "definition": "http://hl7.org/fhir/OperationDefinition/Group-export",
                    },
                    {
                        "name": "everything",
                        "definition": "http://hl7.org/fhir/OperationDefinition/Patient-everything",
                    },
                ],
                # Stated absences. A client reading this learns what not to attempt, rather
                # than learning it from a result set that is quietly wrong.
                "searchInclude": [],
                "searchRevInclude": [],
            }
        ],
    }


def _parameter_type(name: str) -> str:
    if name in ("_id", "identifier"):
        return "token"
    if name in (
        "status",
        "intent",
        "code",
        "category",
        "type",
        "gender",
        "modality",
        "clinical-status",
        "verification-status",
        "priority",
    ):
        return "token"
    if name in ("date", "birthdate", "started", "authored", "authoredon", "onset-date"):
        return "date"
    if name in ("patient", "subject", "encounter"):
        return "reference"
    return "string"
