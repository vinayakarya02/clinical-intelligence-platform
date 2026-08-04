"""SMART-on-FHIR readiness.

Interfaces only, per the phase scope: no live FHIR server is required, and none is contacted.
What is here is the *shape* a SMART app launch takes, so that wiring a real server later is
an implementation of these protocols rather than a redesign.

The launch context and scope model are the parts worth getting right now, because they
determine what the application is allowed to read — and a scope model retrofitted after the
fact is a scope model nobody trusts.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from cip_core.logging import get_logger

__all__ = ["FhirResourceProvider", "LaunchContext", "SmartScope", "parse_scopes"]

_log = get_logger(__name__)


class SmartScope(StrEnum):
    """SMART scopes this platform requests.

    Read-only, and deliberately so: the platform proposes and never writes
    (docs/design/adr-0024-human-approval-gate.md), so requesting write scope would be asking
    for authority it has decided not to exercise.
    """

    PATIENT_READ = "patient/*.read"
    OBSERVATION_READ = "patient/Observation.read"
    MEDICATION_READ = "patient/MedicationRequest.read"
    CONDITION_READ = "patient/Condition.read"
    ALLERGY_READ = "patient/AllergyIntolerance.read"
    LAUNCH = "launch"
    OPENID = "openid"
    FHIR_USER = "fhirUser"


def parse_scopes(granted: str) -> frozenset[str]:
    """Parse a space-delimited scope string.

    Unknown scopes are kept rather than dropped: a server may grant more than was asked for,
    and silently discarding a scope would make a later authorisation failure inexplicable.
    """
    return frozenset(part for part in granted.split() if part)


@dataclass(frozen=True, slots=True)
class LaunchContext:
    """What a SMART app is launched with."""

    patient_id: str
    tenant_id: uuid.UUID
    user_id: str = ""
    encounter_id: str = ""
    fhir_base_url: str = ""
    granted_scopes: frozenset[str] = field(default_factory=frozenset)
    intent: str = ""

    def __post_init__(self) -> None:
        if not self.patient_id.strip():
            raise ValueError("A SMART launch requires a patient context")

    def can_read(self, resource: str) -> bool:
        """Whether the granted scopes permit reading a resource type.

        Wildcard-aware, because ``patient/*.read`` is what most servers actually grant and a
        literal-only check would refuse work the app is authorised for.
        """
        if SmartScope.PATIENT_READ.value in self.granted_scopes:
            return True
        return f"patient/{resource}.read" in self.granted_scopes

    def missing_scopes(self, required: frozenset[str]) -> frozenset[str]:
        if SmartScope.PATIENT_READ.value in self.granted_scopes:
            return frozenset()
        return required - self.granted_scopes


@runtime_checkable
class FhirResourceProvider(Protocol):
    """Reads FHIR resources for a launch context.

    The seam a real FHIR client implements. Deliberately narrow — read a resource type for a
    patient — because that is all the decision engine needs, and a wider interface would bind
    us to one server's capabilities.
    """

    async def read(self, resource_type: str, *, context: LaunchContext) -> list[dict[str, Any]]: ...
