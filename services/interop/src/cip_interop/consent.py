"""The consent engine.

Deny by default, evaluated at the point of disclosure, with break-glass auditing *before* it
returns data (docs/design/adr-0028-consent-deny-by-default.md).

Three refusals define it.

**No consent on file is not permission**, and it is not the same answer as "denied". The first
means somebody must obtain consent; the second means the patient decided. Collapsing them hides
a fixable operational gap behind a patient's choice.

**A purpose is required.** A caller that does not state one is refused rather than defaulted to
treatment. A purpose the system infers is a purpose nobody stated.

**Break-glass writes its audit record first.** If the audit write fails, the disclosure fails.
Otherwise a failure to audit becomes an undetected disclosure, which is the one outcome the
control exists to prevent.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from cip_core.logging import get_logger
from cip_interop.domain import InteropError, PurposeOfUse
from cip_interop.orgs import OrganizationContext

__all__ = [
    "ConsentEngine",
    "ConsentError",
    "ConsentEvaluation",
    "ConsentOutcome",
    "ConsentRecord",
    "Decision",
    "DisclosureRequest",
    "InMemoryAuditSink",
    "Provision",
    "RegionalPolicy",
]

_log = get_logger(__name__)


class ConsentError(InteropError):
    """A consent operation could not be performed."""


class Decision(StrEnum):
    """A permit-or-deny verdict."""

    PERMIT = "permit"
    DENY = "deny"

    @property
    def inverted(self) -> Decision:
        return Decision.DENY if self is Decision.PERMIT else Decision.PERMIT


class ConsentOutcome(StrEnum):
    """What the engine concluded.

    Three outcomes, never two — see the module docstring.
    """

    PERMITTED = "permitted"
    DENIED = "denied"
    NO_CONSENT_ON_FILE = "no_consent_on_file"
    BREAK_GLASS = "break_glass"
    """Permitted despite a denying consent, audited and queued for review."""

    @property
    def discloses(self) -> bool:
        return self in (ConsentOutcome.PERMITTED, ConsentOutcome.BREAK_GLASS)

    @property
    def operational_action(self) -> str:
        """What a human should do about this outcome."""
        return {
            "permitted": "none",
            "denied": "none; the patient has decided",
            "no_consent_on_file": "obtain consent from the patient",
            "break_glass": "review the emergency access",
        }[self.value]


@dataclass(frozen=True, slots=True)
class Provision:
    """An exception to a consent's base decision.

    Empty collections mean *any*. That is the FHIR convention and it is the dangerous
    direction, so the loader in :meth:`ConsentRecord.__post_init__` refuses a permitting
    provision that constrains nothing — a blanket permit dressed as a specific one.
    """

    decision: Decision
    purposes: frozenset[PurposeOfUse] = frozenset()
    actor_ids: frozenset[str] = frozenset()
    actor_organizations: frozenset[str] = frozenset()
    data_categories: frozenset[str] = frozenset()
    period_start: dt.date | None = None
    period_end: dt.date | None = None
    note: str = ""

    @property
    def is_unconstrained(self) -> bool:
        return not (
            self.purposes
            or self.actor_ids
            or self.actor_organizations
            or self.data_categories
            or self.period_start
            or self.period_end
        )

    @property
    def specificity(self) -> int:
        """How many dimensions this provision constrains.

        Used for ordering: a more specific provision wins over a less specific one, which is
        how "deny everything except my cardiologist" resolves the way a patient expects.
        """
        return sum(
            1
            for constraint in (
                self.purposes,
                self.actor_ids,
                self.actor_organizations,
                self.data_categories,
            )
            if constraint
        ) + (1 if self.period_start or self.period_end else 0)

    def applies(
        self,
        *,
        purpose: PurposeOfUse,
        actor_id: str,
        actor_organization: str,
        category: str,
        on: dt.date,
    ) -> bool:
        if self.purposes and purpose not in self.purposes:
            return False
        if self.actor_ids and actor_id not in self.actor_ids:
            return False
        if self.actor_organizations and actor_organization not in self.actor_organizations:
            return False
        if self.data_categories and category not in self.data_categories:
            return False
        if self.period_start and on < self.period_start:
            return False
        return not (self.period_end and on > self.period_end)


@dataclass(frozen=True, slots=True)
class ConsentRecord:
    """One patient's filed consent."""

    consent_id: str
    person_id: str
    organization_id: str
    base_decision: Decision
    provisions: tuple[Provision, ...] = ()
    effective_from: dt.date = field(default_factory=dt.date.today)
    effective_until: dt.date | None = None
    revoked_at: dt.datetime | None = None
    revoked_by: str = ""
    recorded_by: str = ""
    version: int = 1

    def __post_init__(self) -> None:
        if not self.person_id.strip():
            raise ConsentError("ConsentRecord.person_id must not be empty")
        for provision in self.provisions:
            if provision.decision is Decision.PERMIT and provision.is_unconstrained:
                raise ConsentError(
                    f"consent {self.consent_id!r} contains an unconstrained permitting "
                    "provision, which permits everything while appearing to permit something "
                    "specific. Set the base decision instead."
                )

    @property
    def revoked(self) -> bool:
        return self.revoked_at is not None

    def is_in_force(self, on: dt.date, *, now: dt.datetime | None = None) -> bool:
        """Whether this consent governs a disclosure at this moment.

        Revocation is checked against the *disclosure* time, not the consent's date range: a
        consent revoked this morning does not govern this afternoon's request even though its
        effective period still covers today.
        """
        if self.revoked_at is not None:
            moment = now or dt.datetime.now(dt.UTC)
            if moment >= self.revoked_at:
                return False
        if on < self.effective_from:
            return False
        return not (self.effective_until and on > self.effective_until)


@dataclass(frozen=True, slots=True)
class DisclosureRequest:
    """One request to disclose data about one person."""

    person_id: str
    context: OrganizationContext
    purpose: PurposeOfUse
    data_category: str = ""
    holding_organization_id: str = ""
    break_glass_reason: str = ""
    at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))

    def __post_init__(self) -> None:
        if not self.person_id.strip():
            raise ConsentError("DisclosureRequest.person_id must not be empty")

    @property
    def on(self) -> dt.date:
        return self.at.date()


@dataclass(frozen=True, slots=True)
class ConsentEvaluation:
    """The verdict, with the reasoning that produced it."""

    outcome: ConsentOutcome
    reason: str
    consent_id: str = ""
    applied_provision: Provision | None = None
    audit_id: str = ""
    review_required: bool = False

    @property
    def discloses(self) -> bool:
        return self.outcome.discloses

    def render(self) -> str:
        return f"{self.outcome.value}: {self.reason}"

    def to_json(self) -> dict[str, Any]:
        return {
            "outcome": str(self.outcome),
            "reason": self.reason,
            "consent_id": self.consent_id,
            "audit_id": self.audit_id,
            "review_required": self.review_required,
            "next_action": self.outcome.operational_action,
        }


@runtime_checkable
class AuditSink(Protocol):
    """Where disclosure decisions are recorded.

    ``record`` returns the audit id and **may raise**. A raise blocks the disclosure, which is
    the entire point: an unauditable disclosure must not happen.
    """

    def record(self, entry: dict[str, Any]) -> str: ...


@dataclass(slots=True)
class InMemoryAuditSink:
    """An audit sink for tests and single-process runs.

    Bounded, and the bound drops the **oldest** entries — with break-glass entries exempt,
    because those are the ones an investigation will look for and the ones a high-volume
    attacker would try to flush out of the buffer.
    """

    max_entries: int = 100_000
    entries: list[dict[str, Any]] = field(default_factory=list)

    def record(self, entry: dict[str, Any]) -> str:
        audit_id = f"audit:{uuid.uuid4()}"
        self.entries.append({**entry, "audit_id": audit_id})
        if len(self.entries) > self.max_entries:
            protected = [e for e in self.entries if e.get("break_glass")]
            ordinary = [e for e in self.entries if not e.get("break_glass")]
            keep = max(0, self.max_entries - len(protected))
            self.entries = protected + ordinary[-keep:] if keep else protected
        return audit_id

    def break_glass_entries(self) -> list[dict[str, Any]]:
        return [e for e in self.entries if e.get("break_glass")]


@dataclass(frozen=True, slots=True)
class RegionalPolicy:
    """A jurisdiction's additional restrictions.

    Layers on top of consent and can **only further restrict**. A regional rule that widened
    access would let a deployment configure its way out of a patient's decision, so the engine
    applies these after consent and only in the denying direction.
    """

    region: str
    forbidden_purposes: frozenset[PurposeOfUse] = frozenset()
    categories_requiring_explicit_consent: frozenset[str] = frozenset()
    """Categories where a permissive *base decision* is not enough — 42 CFR Part 2 substance
    use disorder records and equivalent regimes require a specific provision naming them."""
    note: str = ""

    def restricts(self, *, purpose: PurposeOfUse, category: str) -> str:
        if purpose in self.forbidden_purposes:
            return f"{self.region} forbids disclosure for purpose {purpose.value}"
        if category and category in self.categories_requiring_explicit_consent:
            return (
                f"{self.region} requires an explicit provision naming category {category!r}; "
                "a general permission is not sufficient"
            )
        return ""


class ConsentEngine:
    """Evaluates disclosure requests against filed consents."""

    def __init__(
        self,
        *,
        audit_sink: AuditSink,
        regional_policies: dict[str, RegionalPolicy] | None = None,
    ) -> None:
        self._audit = audit_sink
        self._policies = regional_policies or {}
        self._consents: dict[str, list[ConsentRecord]] = {}

    def file(self, consent: ConsentRecord) -> None:
        """File a consent. Later versions supersede earlier ones for the same person."""
        self._consents.setdefault(consent.person_id, []).append(consent)

    def revoke(
        self, consent_id: str, *, revoked_by: str, at: dt.datetime | None = None
    ) -> ConsentRecord:
        """Revoke a consent, effective immediately and forward only.

        A revocation cannot unmake earlier disclosures, and the engine does not pretend it can:
        the audit trail keeps both the disclosure and the later revocation rather than rewriting
        history to look compliant.
        """
        if not revoked_by.strip():
            raise ConsentError("a revocation requires a named actor")
        moment = at or dt.datetime.now(dt.UTC)
        for person_id, records in self._consents.items():
            for index, record in enumerate(records):
                if record.consent_id != consent_id:
                    continue
                if record.revoked:
                    raise ConsentError(f"consent {consent_id!r} is already revoked")
                revoked = ConsentRecord(
                    consent_id=record.consent_id,
                    person_id=record.person_id,
                    organization_id=record.organization_id,
                    base_decision=record.base_decision,
                    provisions=record.provisions,
                    effective_from=record.effective_from,
                    effective_until=record.effective_until,
                    revoked_at=moment,
                    revoked_by=revoked_by,
                    recorded_by=record.recorded_by,
                    version=record.version,
                )
                self._consents[person_id][index] = revoked
                self._audit.record(
                    {
                        "action": "consent.revoked",
                        "consent_id": consent_id,
                        "person_id": person_id,
                        "by": revoked_by,
                        "at": moment.isoformat(),
                    }
                )
                return revoked
        raise ConsentError(f"unknown consent {consent_id!r}")

    def consents_for(self, person_id: str) -> tuple[ConsentRecord, ...]:
        return tuple(self._consents.get(person_id, ()))

    def evaluate(self, request: DisclosureRequest) -> ConsentEvaluation:
        """Decide, audit, and return.

        Every outcome is audited, not only the denials. A disclosure log that records only
        refusals cannot answer "who saw this patient's record", which is the question HIPAA
        §164.312(b) exists to make answerable.
        """
        if request.purpose.requires_named_human and not request.context.is_named_human:
            return self._audited(
                request,
                ConsentEvaluation(
                    outcome=ConsentOutcome.DENIED,
                    reason=(
                        f"purpose {request.purpose.value} requires a named human principal; "
                        f"{request.context.principal_id!r} is a service account"
                    ),
                ),
            )

        if request.purpose.is_break_glass:
            return self._break_glass(request)

        applicable = [
            c
            for c in self._consents.get(request.person_id, ())
            if c.is_in_force(request.on, now=request.at)
            and (
                not request.holding_organization_id
                or c.organization_id in ("", request.holding_organization_id)
            )
        ]
        if not applicable:
            return self._audited(
                request,
                ConsentEvaluation(
                    outcome=ConsentOutcome.NO_CONSENT_ON_FILE,
                    reason=(
                        f"no consent in force for {request.person_id} at "
                        f"{request.holding_organization_id or 'any organisation'}"
                    ),
                ),
            )

        consent = max(applicable, key=lambda c: (c.version, c.effective_from))
        evaluation = self._apply(consent, request)

        if evaluation.outcome is ConsentOutcome.PERMITTED:
            region = self._policies.get(request.context.organization_id) or self._policies.get(
                request.holding_organization_id
            )
            if region:
                restriction = region.restricts(
                    purpose=request.purpose, category=request.data_category
                )
                if restriction:
                    evaluation = ConsentEvaluation(
                        outcome=ConsentOutcome.DENIED,
                        reason=restriction,
                        consent_id=consent.consent_id,
                    )

        return self._audited(request, evaluation)

    def _apply(self, consent: ConsentRecord, request: DisclosureRequest) -> ConsentEvaluation:
        matching = [
            provision
            for provision in consent.provisions
            if provision.applies(
                purpose=request.purpose,
                actor_id=request.context.principal_id,
                actor_organization=request.context.organization_id,
                category=request.data_category,
                on=request.on,
            )
        ]
        if matching:
            # Most specific wins; a tie goes to deny. When a patient's consent is ambiguous,
            # the safe reading is the restrictive one — a wrongly-withheld record is recoverable
            # by asking, a wrongly-disclosed one is not.
            best = max(
                matching,
                key=lambda p: (p.specificity, 1 if p.decision is Decision.DENY else 0),
            )
            outcome = (
                ConsentOutcome.PERMITTED
                if best.decision is Decision.PERMIT
                else ConsentOutcome.DENIED
            )
            return ConsentEvaluation(
                outcome=outcome,
                reason=(
                    f"provision ({best.decision.value}) applies for purpose "
                    f"{request.purpose.value}" + (f": {best.note}" if best.note else "")
                ),
                consent_id=consent.consent_id,
                applied_provision=best,
            )

        outcome = (
            ConsentOutcome.PERMITTED
            if consent.base_decision is Decision.PERMIT
            else ConsentOutcome.DENIED
        )
        return ConsentEvaluation(
            outcome=outcome,
            reason=f"base decision ({consent.base_decision.value}); no provision applies",
            consent_id=consent.consent_id,
        )

    def _break_glass(self, request: DisclosureRequest) -> ConsentEvaluation:
        """Emergency access.

        The audit record is written **before** the evaluation is returned, so a sink failure
        propagates as a refusal rather than as a silent, unlogged disclosure.
        """
        if not request.context.is_named_human:
            return self._audited(
                request,
                ConsentEvaluation(
                    outcome=ConsentOutcome.DENIED,
                    reason="break-glass requires a named human principal, not a service account",
                ),
            )
        if not request.break_glass_reason.strip():
            return self._audited(
                request,
                ConsentEvaluation(
                    outcome=ConsentOutcome.DENIED,
                    reason=(
                        "break-glass requires a stated reason; unexplained emergency access is "
                        "indistinguishable from no access control"
                    ),
                ),
            )

        try:
            audit_id = self._audit.record(
                {
                    "action": "disclosure.break_glass",
                    "severity": "high",
                    "break_glass": True,
                    "person_id": request.person_id,
                    "principal_id": request.context.principal_id,
                    "organization_id": request.context.organization_id,
                    "holding_organization_id": request.holding_organization_id,
                    "purpose": request.purpose.value,
                    "data_category": request.data_category,
                    "reason": request.break_glass_reason,
                    "at": request.at.isoformat(),
                }
            )
        except Exception as exc:
            _log.error("consent.break_glass_audit_failed", error=type(exc).__name__)
            return ConsentEvaluation(
                outcome=ConsentOutcome.DENIED,
                reason=(
                    f"break-glass refused: the audit record could not be written "
                    f"({type(exc).__name__}). An unauditable emergency disclosure is not "
                    "permitted."
                ),
            )

        _log.warning(
            "consent.break_glass",
            person=request.person_id,
            principal=request.context.principal_id,
            audit_id=audit_id,
        )
        return ConsentEvaluation(
            outcome=ConsentOutcome.BREAK_GLASS,
            reason=f"emergency access granted and audited: {request.break_glass_reason}",
            audit_id=audit_id,
            review_required=True,
        )

    def _audited(
        self, request: DisclosureRequest, evaluation: ConsentEvaluation
    ) -> ConsentEvaluation:
        """Record the decision. A sink failure denies rather than discloses."""
        if evaluation.audit_id:
            return evaluation
        try:
            audit_id = self._audit.record(
                {
                    "action": "disclosure.evaluated",
                    "outcome": str(evaluation.outcome),
                    "person_id": request.person_id,
                    "principal_id": request.context.principal_id,
                    "organization_id": request.context.organization_id,
                    "holding_organization_id": request.holding_organization_id,
                    "purpose": request.purpose.value,
                    "data_category": request.data_category,
                    "consent_id": evaluation.consent_id,
                    "at": request.at.isoformat(),
                }
            )
        except Exception as exc:
            _log.error("consent.audit_failed", error=type(exc).__name__)
            return ConsentEvaluation(
                outcome=ConsentOutcome.DENIED,
                reason=(
                    f"refused: the disclosure could not be audited ({type(exc).__name__}). "
                    "An unauditable disclosure is not permitted."
                ),
                consent_id=evaluation.consent_id,
            )
        return ConsentEvaluation(
            outcome=evaluation.outcome,
            reason=evaluation.reason,
            consent_id=evaluation.consent_id,
            applied_provision=evaluation.applied_provision,
            audit_id=audit_id,
            review_required=evaluation.review_required,
        )
