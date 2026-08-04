"""Organisations, facilities, and the agreements that permit sharing between them.

Phases 1–5 had one tenant boundary. This phase has a nested hierarchy — health system,
hospital, department, lab, imaging centre — and one relationship that is *not* nesting:
cross-organisation sharing.

The load-bearing rule is that **sharing is never inferred**. Two organisations holding records
for the same resolved person share nothing by default. Disclosure needs an explicit, dated,
purpose-scoped agreement, and the EMPI's opinion that two records are one person is not one
(docs/design/adr-0030-cross-organisation-sharing.md).

Agreements are dated in both directions, so a partnership that ends stops working on its expiry
date rather than running for years after because nobody remembered to disable it.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from cip_core.logging import get_logger
from cip_interop.domain import InteropError, PurposeOfUse

__all__ = [
    "AgreementRegistry",
    "Organization",
    "OrganizationDirectory",
    "OrganizationKind",
    "SharingAgreement",
    "SharingDecision",
]

_log = get_logger(__name__)


class OrganizationKind(StrEnum):
    """What kind of thing an organisation is.

    Drives nothing on its own — it is metadata for humans and for routing — but a lab and a
    hospital are different tenants with different agreements, and typing them keeps the
    directory honest about which is which.
    """

    HEALTH_SYSTEM = "health_system"
    HOSPITAL = "hospital"
    DEPARTMENT = "department"
    LABORATORY = "laboratory"
    IMAGING_CENTER = "imaging_center"
    PHARMACY = "pharmacy"
    CLINIC = "clinic"
    PAYER = "payer"
    RESEARCH = "research"

    @property
    def is_care_delivery(self) -> bool:
        return self in (
            OrganizationKind.HOSPITAL,
            OrganizationKind.DEPARTMENT,
            OrganizationKind.CLINIC,
        )


@dataclass(frozen=True, slots=True)
class Organization:
    """One organisation or facility."""

    organization_id: str
    name: str
    kind: OrganizationKind
    parent_id: str = ""
    region: str = ""
    """Jurisdiction, which selects the regional policy layer. Empty means no additional
    regional restriction applies — which is a decision, so it is recorded rather than
    defaulted silently."""
    active: bool = True

    def __post_init__(self) -> None:
        if not self.organization_id.strip():
            raise InteropError("Organization.organization_id must not be empty")
        if self.parent_id == self.organization_id:
            raise InteropError(f"Organization {self.organization_id!r} cannot be its own parent")


class OrganizationDirectory:
    """The organisation hierarchy.

    A tree, and the tree is validated on insert: a cycle would make ancestry queries loop
    forever, and ancestry is consulted on the authorisation path.
    """

    def __init__(self) -> None:
        self._organizations: dict[str, Organization] = {}

    def register(self, organization: Organization) -> None:
        if organization.organization_id in self._organizations:
            raise InteropError(f"organisation {organization.organization_id!r} already registered")
        if organization.parent_id and organization.parent_id not in self._organizations:
            raise InteropError(
                f"organisation {organization.organization_id!r} declares parent "
                f"{organization.parent_id!r}, which is not registered. Registering the child "
                "first would leave an unreachable subtree."
            )
        self._organizations[organization.organization_id] = organization

    def get(self, organization_id: str) -> Organization:
        organization = self._organizations.get(organization_id)
        if organization is None:
            raise InteropError(f"unknown organisation {organization_id!r}")
        return organization

    def exists(self, organization_id: str) -> bool:
        return organization_id in self._organizations

    def ancestors(self, organization_id: str) -> tuple[str, ...]:
        """Every ancestor, nearest first.

        Bounded by the number of registered organisations, so a cycle introduced by some future
        mutation path terminates rather than hanging the authorisation check.
        """
        found: list[str] = []
        seen: set[str] = set()
        current = self.get(organization_id).parent_id
        while current and current not in seen and len(found) <= len(self._organizations):
            seen.add(current)
            found.append(current)
            current = (
                self._organizations[current].parent_id if current in self._organizations else ""
            )
        return tuple(found)

    def descendants(self, organization_id: str) -> tuple[str, ...]:
        self.get(organization_id)
        found: list[str] = []
        frontier = [organization_id]
        while frontier:
            parent = frontier.pop()
            for candidate in self._organizations.values():
                if candidate.parent_id == parent and candidate.organization_id not in found:
                    found.append(candidate.organization_id)
                    frontier.append(candidate.organization_id)
        return tuple(sorted(found))

    def within_same_organization(self, left: str, right: str) -> bool:
        """Whether two units belong to one organisation.

        True when one contains the other. **Sibling departments are not the same organisation**
        for sharing purposes even though they share a parent — a psychiatry department and a
        cardiology department under one hospital have different disclosure expectations, and
        collapsing them is how sensitive notes travel.
        """
        if left == right:
            return True
        return right in self.ancestors(left) or left in self.ancestors(right)

    def all(self) -> tuple[Organization, ...]:
        return tuple(sorted(self._organizations.values(), key=lambda o: o.organization_id))

    def statistics(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for organization in self._organizations.values():
            counts[organization.kind.value] = counts.get(organization.kind.value, 0) + 1
        return {"organizations": len(self._organizations), "by_kind": counts}


@dataclass(frozen=True, slots=True)
class SharingAgreement:
    """A dated, purpose-scoped permission for one organisation to receive another's data.

    Directional. An agreement letting a hospital send results to a referring clinic does not let
    the clinic read the hospital's whole record, and modelling it as symmetric is how that
    happens.
    """

    agreement_id: str
    source_organization_id: str
    """The organisation whose data may be disclosed."""
    target_organization_id: str
    """The organisation that may receive it."""
    purposes: frozenset[PurposeOfUse]
    effective_from: dt.date
    effective_until: dt.date | None = None
    data_categories: frozenset[str] = frozenset()
    """Empty means every category. Populated means only these — a referral agreement covering
    laboratory results does not cover psychiatry notes."""
    signed_by: str = ""
    note: str = ""

    def __post_init__(self) -> None:
        if self.source_organization_id == self.target_organization_id:
            raise InteropError(
                "a sharing agreement between an organisation and itself is meaningless; "
                "intra-organisation access is governed by role, not by agreement"
            )
        if not self.purposes:
            raise InteropError(
                f"agreement {self.agreement_id!r} names no purpose. An agreement permitting "
                "everything is not an agreement."
            )
        if self.effective_until and self.effective_until < self.effective_from:
            raise InteropError(f"agreement {self.agreement_id!r} expires before it starts")
        if not self.signed_by.strip():
            raise InteropError(
                f"agreement {self.agreement_id!r} records no signatory; an unattributed "
                "agreement cannot be audited or withdrawn"
            )

    def is_active(self, on: dt.date) -> bool:
        if on < self.effective_from:
            return False
        return not (self.effective_until and on > self.effective_until)

    def covers(self, purpose: PurposeOfUse, category: str) -> bool:
        if purpose not in self.purposes:
            return False
        return not self.data_categories or category in self.data_categories


@dataclass(frozen=True, slots=True)
class SharingDecision:
    """Whether an agreement permits a disclosure, and if not, precisely why not.

    The reason matters operationally: an operator needs to know whether to chase an agreement,
    extend an expired one, or widen its purposes. "Denied" tells them none of that.
    """

    permitted: bool
    reason: str
    agreement_id: str = ""

    def render(self) -> str:
        verdict = "permitted" if self.permitted else "refused"
        via = f" via {self.agreement_id}" if self.agreement_id else ""
        return f"{verdict}{via}: {self.reason}"


class AgreementRegistry:
    """The agreements in force."""

    def __init__(self, directory: OrganizationDirectory) -> None:
        self._directory = directory
        self._agreements: dict[str, SharingAgreement] = {}

    def register(self, agreement: SharingAgreement) -> None:
        for organization_id in (
            agreement.source_organization_id,
            agreement.target_organization_id,
        ):
            if not self._directory.exists(organization_id):
                raise InteropError(
                    f"agreement {agreement.agreement_id!r} names unregistered organisation "
                    f"{organization_id!r}"
                )
        self._agreements[agreement.agreement_id] = agreement

    def evaluate(
        self,
        *,
        source_organization_id: str,
        target_organization_id: str,
        purpose: PurposeOfUse,
        category: str = "",
        on: dt.date | None = None,
    ) -> SharingDecision:
        """Whether ``target`` may receive ``source``'s data for ``purpose``.

        An intra-organisation request needs no agreement — role-based access governs it — and
        this returns permitted with that stated, so a caller can tell "no agreement was needed"
        from "an agreement allowed it".
        """
        today = on or dt.date.today()

        for organization_id in (source_organization_id, target_organization_id):
            if not self._directory.exists(organization_id):
                # A refusal, not an exception. This runs on the authorisation path, and an
                # unhandled error there is a 500 where a 403 was meant — which fails open in
                # any caller that catches broadly.
                return SharingDecision(
                    permitted=False,
                    reason=f"organisation {organization_id!r} is not registered",
                )

        if self._directory.within_same_organization(source_organization_id, target_organization_id):
            return SharingDecision(
                permitted=True,
                reason="same organisation; governed by role rather than by agreement",
            )

        candidates = [
            a
            for a in self._agreements.values()
            if a.source_organization_id == source_organization_id
            and a.target_organization_id == target_organization_id
        ]
        if not candidates:
            return SharingDecision(
                permitted=False,
                reason=(
                    f"no sharing agreement exists from {source_organization_id} to "
                    f"{target_organization_id}"
                ),
            )

        expired = []
        wrong_purpose = []
        for agreement in candidates:
            if not agreement.is_active(today):
                expired.append(agreement.agreement_id)
                continue
            if not agreement.covers(purpose, category):
                wrong_purpose.append(agreement.agreement_id)
                continue
            return SharingDecision(
                permitted=True,
                reason=f"agreement covers {purpose.value}",
                agreement_id=agreement.agreement_id,
            )

        if wrong_purpose:
            return SharingDecision(
                permitted=False,
                reason=(
                    f"agreement {wrong_purpose[0]} is in force but does not cover purpose "
                    f"{purpose.value}" + (f" for category {category!r}" if category else "")
                ),
                agreement_id=wrong_purpose[0],
            )
        return SharingDecision(
            permitted=False,
            reason=f"agreement {expired[0]} is not in force on {today.isoformat()}",
            agreement_id=expired[0],
        )

    def active_agreements(self, on: dt.date | None = None) -> tuple[SharingAgreement, ...]:
        today = on or dt.date.today()
        return tuple(a for a in self._agreements.values() if a.is_active(today))

    def expiring_within(
        self, days: int, *, on: dt.date | None = None
    ) -> tuple[SharingAgreement, ...]:
        """Agreements about to lapse.

        Worth monitoring: an agreement expiring silently stops a clinical interface, and the
        first symptom is a clinician not seeing results they expected.
        """
        today = on or dt.date.today()
        horizon = today + dt.timedelta(days=days)
        return tuple(
            a
            for a in self._agreements.values()
            if a.effective_until and today <= a.effective_until <= horizon
        )


@dataclass(slots=True)
class OrganizationContext:
    """Who is asking, from where.

    Threaded through every disclosure. Deliberately a required argument rather than ambient
    state: a context that can be forgotten is one that will be, and the resulting query runs
    unscoped.
    """

    principal_id: str
    organization_id: str
    roles: frozenset[str] = field(default_factory=frozenset)
    is_named_human: bool = True
    """``False`` for service accounts. Break-glass and emergency treatment refuse them, because
    both exist to be answered for and a service account cannot be asked why."""
    session_id: str = ""
