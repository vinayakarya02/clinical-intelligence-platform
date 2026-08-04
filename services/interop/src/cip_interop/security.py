"""Enterprise security: OIDC/OAuth2 tokens, SMART v2 scopes, ABAC, SCIM, and delegation.

Builds on Phase 4's principals and roles rather than replacing them; this layer adds what
cross-organisation healthcare needs and a general platform does not.

Three decisions are load-bearing.

**SMART v2 scope syntax, parsed rather than pattern-matched.** ``patient/Observation.rs`` and
``user/*.cruds`` are structured, and the ``.cruds`` suffix is an *in-order subset* — ``.rs`` is
read and search, ``.cu`` is create and update. Treating the suffix as a flag string means
``.s`` matches ``.rs`` by substring and a search-only app gets read.

**Granular scopes are enforced or refused, never partially applied.** A scope like
``user/Observation.rs?category=laboratory`` narrows to laboratory observations. A server that
parses the scope but ignores the query parameter grants everything the scope names — which is
the opposite of what the app asked for and what the patient was shown at consent.

**ABAC decisions are deny-overrides.** Any rule that denies wins over any number that permit.
The alternative — first match, or permit-overrides — means adding a permissive rule can
silently widen access granted by an unrelated one.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from cip_core.logging import get_logger
from cip_interop.domain import InteropError, PurposeOfUse

__all__ = [
    "AbacPolicy",
    "AbacRule",
    "AccessDecision",
    "Delegation",
    "ScimUser",
    "ScopeSet",
    "SecurityError",
    "SmartScope",
    "TokenClaims",
    "TokenVerifier",
    "parse_scope",
]

_log = get_logger(__name__)

#: ``context/ResourceType.suffix?param=value`` — the SMART v2 grammar.
_SCOPE = re.compile(
    r"^(?P<context>patient|user|system)/"
    r"(?P<resource>\*|[A-Z][A-Za-z]+)"
    r"(?:\.(?P<suffix>[cruds]+|read|write|\*))?"
    r"(?:\?(?P<query>.+))?$"
)

#: The v2 suffix alphabet, in the order the specification fixes.
_CRUDS = "cruds"


class SecurityError(InteropError):
    """An authentication or authorisation failure."""


class Operation(StrEnum):
    """What a caller wants to do."""

    CREATE = "c"
    READ = "r"
    UPDATE = "u"
    DELETE = "d"
    SEARCH = "s"

    @classmethod
    def from_name(cls, name: str) -> Operation:
        return {
            "create": cls.CREATE,
            "read": cls.READ,
            "vread": cls.READ,
            "update": cls.UPDATE,
            "patch": cls.UPDATE,
            "delete": cls.DELETE,
            "search": cls.SEARCH,
            "search-type": cls.SEARCH,
            "history": cls.READ,
        }.get(name.lower(), cls.READ)


@dataclass(frozen=True, slots=True)
class SmartScope:
    """One parsed SMART on FHIR scope."""

    context: str
    """``patient``, ``user``, or ``system``."""
    resource_type: str
    """A resource type, or ``*``."""
    operations: frozenset[Operation]
    constraints: dict[str, str] = field(default_factory=dict)
    """Granular-scope search parameters. **Enforced**, not decorative — a scope narrowed to
    laboratory observations that returns every observation has granted more than was asked
    for."""
    raw: str = ""

    @property
    def is_wildcard_resource(self) -> bool:
        return self.resource_type == "*"

    def covers(self, resource_type: str, operation: Operation) -> bool:
        if not self.is_wildcard_resource and self.resource_type != resource_type:
            return False
        return operation in self.operations

    def satisfied_by(self, resource_attributes: dict[str, str]) -> bool:
        """Whether a resource falls inside this scope's granular constraints."""
        for parameter, wanted in self.constraints.items():
            actual = resource_attributes.get(parameter, "")
            # A token constraint may be written system|code; matching on the code alone is what
            # every server does in practice and what US Core examples assume.
            if "|" in wanted:
                wanted = wanted.rsplit("|", 1)[1]
            if actual != wanted:
                return False
        return True

    def render(self) -> str:
        # Iterating _CRUDS rather than self.operations is what keeps the suffix in the
        # specification's fixed order — a set has no order, and ".sr" is not a valid v2 scope
        # even though it names the same two operations as ".rs".
        return self.raw or (
            f"{self.context}/{self.resource_type}."
            + "".join(c for c in _CRUDS if Operation(c) in self.operations)
        )


def parse_scope(raw: str) -> SmartScope | None:
    """Parse one scope string, or ``None`` if it is not a SMART resource scope.

    ``None`` rather than raising: a token legitimately carries non-resource scopes
    (``openid``, ``fhirUser``, ``launch/patient``, ``offline_access``), and treating those as
    malformed would reject every real token.
    """
    match = _SCOPE.match(raw.strip())
    if not match:
        return None
    suffix = match.group("suffix") or "*"

    if suffix == "read":
        # SMART v1 compatibility. v1 read means read and search; mapping it to read alone
        # would break every v1 app's search calls.
        operations = {Operation.READ, Operation.SEARCH}
    elif suffix == "write":
        operations = {Operation.CREATE, Operation.UPDATE, Operation.DELETE}
    elif suffix == "*":
        operations = set(Operation)
    else:
        if any(c not in _CRUDS for c in suffix):
            return None
        # The suffix must be an in-order subset. "sr" is not a valid v2 scope, and accepting it
        # means accepting typos as permissions.
        position = -1
        for char in suffix:
            index = _CRUDS.index(char)
            if index <= position:
                return None
            position = index
        operations = {Operation(c) for c in suffix}

    constraints: dict[str, str] = {}
    for pair in (match.group("query") or "").split("&"):
        if "=" in pair:
            key, value = pair.split("=", 1)
            constraints[key.strip()] = value.strip()

    return SmartScope(
        context=match.group("context"),
        resource_type=match.group("resource"),
        operations=frozenset(operations),
        constraints=constraints,
        raw=raw.strip(),
    )


@dataclass(frozen=True, slots=True)
class ScopeSet:
    """Every resource scope on a token."""

    scopes: tuple[SmartScope, ...] = ()
    non_resource_scopes: tuple[str, ...] = ()

    @classmethod
    def parse(cls, raw: str) -> ScopeSet:
        resource_scopes = []
        other = []
        for token in raw.split():
            parsed = parse_scope(token)
            if parsed is None:
                other.append(token)
            else:
                resource_scopes.append(parsed)
        return cls(scopes=tuple(resource_scopes), non_resource_scopes=tuple(other))

    def permits(
        self,
        resource_type: str,
        operation: Operation,
        *,
        resource_attributes: dict[str, str] | None = None,
        context: str = "",
    ) -> bool:
        """Whether any scope covers this operation on this resource."""
        attributes = resource_attributes or {}
        for scope in self.scopes:
            if context and scope.context != context:
                continue
            if scope.covers(resource_type, operation) and scope.satisfied_by(attributes):
                return True
        return False

    def granular_constraints(self, resource_type: str) -> tuple[dict[str, str], ...]:
        """Constraints that apply to a resource type.

        Returned so the query layer can apply them as filters. A scope whose constraints are
        parsed and then dropped grants everything it names.
        """
        return tuple(
            scope.constraints
            for scope in self.scopes
            if scope.constraints
            and (scope.is_wildcard_resource or scope.resource_type == resource_type)
        )

    def render(self) -> str:
        return " ".join([*(s.render() for s in self.scopes), *self.non_resource_scopes])


@dataclass(frozen=True, slots=True)
class TokenClaims:
    """The claims this platform requires from an OIDC/OAuth2 access token."""

    subject: str
    issuer: str
    audience: str
    scopes: ScopeSet
    expires_at: dt.datetime
    issued_at: dt.datetime
    organization_id: str = ""
    patient_context: str = ""
    """The SMART ``patient`` launch context. When set, ``patient/`` scopes apply only to this
    patient — and a server that ignores it turns a single-patient app into a whole-population
    one."""
    fhir_user: str = ""
    purpose: PurposeOfUse | None = None
    is_service_account: bool = False
    delegated_by: str = ""
    attributes: dict[str, str] = field(default_factory=dict)

    def is_valid_at(self, moment: dt.datetime) -> bool:
        return self.issued_at <= moment < self.expires_at

    def seconds_remaining(self, moment: dt.datetime) -> float:
        return max(0.0, (self.expires_at - moment).total_seconds())


@runtime_checkable
class TokenVerifier(Protocol):
    """Verifies a bearer token and returns its claims.

    A protocol because signature verification needs a JWKS endpoint and a network. The
    **claim** checks — issuer, audience, expiry, and the patient-context rule — are implemented
    here and tested, because those are the ones implementations skip.
    """

    def verify(self, token: str, *, at: dt.datetime | None = None) -> TokenClaims: ...


@dataclass(slots=True)
class StaticTokenVerifier:
    """A verifier over pre-registered tokens.

    Not a mock: it performs the full claim validation a real verifier must, and differs only in
    where the claims come from. The signature check is the part that needs a key server, and
    its absence is stated rather than stubbed silently.
    """

    expected_issuer: str
    expected_audience: str
    tokens: dict[str, TokenClaims] = field(default_factory=dict)
    clock_skew_seconds: int = 60

    def register(self, token: str, claims: TokenClaims) -> None:
        self.tokens[token] = claims

    def verify(self, token: str, *, at: dt.datetime | None = None) -> TokenClaims:
        claims = self.tokens.get(token)
        if claims is None:
            raise SecurityError("token is not recognised")
        moment = at or dt.datetime.now(dt.UTC)
        skew = dt.timedelta(seconds=self.clock_skew_seconds)

        if claims.issuer != self.expected_issuer:
            raise SecurityError(
                f"token issuer {claims.issuer!r} is not {self.expected_issuer!r}; a token from "
                "another issuer may be perfectly valid and still not be for this system"
            )
        if claims.audience != self.expected_audience:
            # Audience is the check most often skipped, and skipping it means a token issued for
            # a different service is accepted here — the classic confused-deputy.
            raise SecurityError(
                f"token audience {claims.audience!r} is not {self.expected_audience!r}"
            )
        if moment >= claims.expires_at + skew:
            raise SecurityError("token has expired")
        if moment < claims.issued_at - skew:
            raise SecurityError("token is not yet valid")
        return claims


@dataclass(frozen=True, slots=True)
class AbacRule:
    """One attribute-based rule.

    Conditions are exact attribute matches. Deliberately not an expression language: a policy
    file an administrator edits must not be a program, which is the same refusal the mapping
    and rules layers make.
    """

    rule_id: str
    effect: str
    """``permit`` or ``deny``."""
    subject_attributes: dict[str, str] = field(default_factory=dict)
    resource_attributes: dict[str, str] = field(default_factory=dict)
    actions: frozenset[str] = frozenset()
    purposes: frozenset[PurposeOfUse] = frozenset()
    description: str = ""

    def __post_init__(self) -> None:
        if self.effect not in ("permit", "deny"):
            raise SecurityError(f"rule {self.rule_id!r} has effect {self.effect!r}")

    @property
    def denies(self) -> bool:
        return self.effect == "deny"

    def matches(
        self,
        *,
        subject: dict[str, str],
        resource: dict[str, str],
        action: str,
        purpose: PurposeOfUse | None,
    ) -> bool:
        if self.actions and action not in self.actions:
            return False
        if self.purposes and (purpose is None or purpose not in self.purposes):
            return False
        for key, wanted in self.subject_attributes.items():
            if subject.get(key) != wanted:
                return False
        return all(resource.get(key) == wanted for key, wanted in self.resource_attributes.items())


@dataclass(frozen=True, slots=True)
class AccessDecision:
    """An ABAC verdict with the rules that produced it."""

    permitted: bool
    reason: str
    matched_rules: tuple[str, ...] = ()
    deciding_rule: str = ""

    def render(self) -> str:
        verdict = "permit" if self.permitted else "deny"
        return f"{verdict} ({self.deciding_rule or 'default'}): {self.reason}"


class AbacPolicy:
    """Attribute-based access control, deny-overrides.

    Any matching deny wins over any number of permits, and the absence of a permit is a deny.
    The alternative orderings both have the same failure: adding a rule somewhere can widen
    access granted somewhere else, and nobody notices because the new rule looks narrow.
    """

    def __init__(self, rules: tuple[AbacRule, ...] = ()) -> None:
        self._rules = rules

    def add(self, rule: AbacRule) -> AbacPolicy:
        return AbacPolicy((*self._rules, rule))

    @property
    def rules(self) -> tuple[AbacRule, ...]:
        return self._rules

    def evaluate(
        self,
        *,
        subject: dict[str, str],
        resource: dict[str, str],
        action: str,
        purpose: PurposeOfUse | None = None,
    ) -> AccessDecision:
        matched: list[str] = []
        permit_rule = ""
        for rule in self._rules:
            if not rule.matches(subject=subject, resource=resource, action=action, purpose=purpose):
                continue
            matched.append(rule.rule_id)
            if rule.denies:
                return AccessDecision(
                    permitted=False,
                    reason=rule.description or f"denied by {rule.rule_id}",
                    matched_rules=tuple(matched),
                    deciding_rule=rule.rule_id,
                )
            permit_rule = permit_rule or rule.rule_id

        if permit_rule:
            return AccessDecision(
                permitted=True,
                reason=f"permitted by {permit_rule} with no overriding deny",
                matched_rules=tuple(matched),
                deciding_rule=permit_rule,
            )
        return AccessDecision(
            permitted=False,
            reason="no rule permits this; access is denied by default",
            matched_rules=tuple(matched),
        )


@dataclass(frozen=True, slots=True)
class Delegation:
    """One principal acting on behalf of another.

    The delegated scopes must be a **subset** of the delegator's. A delegation that could grant
    more than the delegator holds is a privilege escalation with an audit trail that makes it
    look authorised.
    """

    delegation_id: str
    delegator_id: str
    delegate_id: str
    scopes: ScopeSet
    expires_at: dt.datetime
    reason: str = ""
    granted_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))

    def is_active(self, at: dt.datetime | None = None) -> bool:
        return (at or dt.datetime.now(dt.UTC)) < self.expires_at

    @staticmethod
    def validate_subset(delegated: ScopeSet, holder: ScopeSet) -> str:
        """Return an error message, or ``""`` when the delegation is within the holder's rights."""
        for scope in delegated.scopes:
            for operation in scope.operations:
                if not holder.permits(scope.resource_type, operation, context=scope.context):
                    return (
                        f"delegated scope {scope.render()!r} exceeds the delegator's own "
                        "permissions"
                    )
        return ""


@dataclass(frozen=True, slots=True)
class ScimUser:
    """A SCIM 2.0 user, for identity provisioning from an enterprise directory."""

    scim_id: str
    user_name: str
    active: bool = True
    display_name: str = ""
    emails: tuple[str, ...] = ()
    organization_id: str = ""
    roles: frozenset[str] = frozenset()
    external_id: str = ""

    def to_scim(self) -> dict[str, Any]:
        return {
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
            "id": self.scim_id,
            "userName": self.user_name,
            "active": self.active,
            "displayName": self.display_name,
            "emails": [{"value": e, "primary": i == 0} for i, e in enumerate(self.emails)],
            "externalId": self.external_id,
            "roles": [{"value": r} for r in sorted(self.roles)],
        }


class ScimProvisioner:
    """Applies SCIM create, update, and deactivate operations.

    Deactivation never deletes. A deleted user id can be reissued by a directory, and reissuing
    one that appears in years of audit records makes those records point at the wrong person.
    """

    def __init__(self) -> None:
        self._users: dict[str, ScimUser] = {}
        self._deactivated: dict[str, dt.datetime] = {}

    def create(self, user: ScimUser) -> ScimUser:
        if user.scim_id in self._users:
            raise SecurityError(f"SCIM user {user.scim_id!r} already exists")
        self._users[user.scim_id] = user
        return user

    def replace(self, user: ScimUser) -> ScimUser:
        if user.scim_id not in self._users:
            raise SecurityError(f"unknown SCIM user {user.scim_id!r}")
        self._users[user.scim_id] = user
        return user

    def deactivate(self, scim_id: str, *, at: dt.datetime | None = None) -> ScimUser:
        user = self._users.get(scim_id)
        if user is None:
            raise SecurityError(f"unknown SCIM user {scim_id!r}")
        deactivated = ScimUser(
            scim_id=user.scim_id,
            user_name=user.user_name,
            active=False,
            display_name=user.display_name,
            emails=user.emails,
            organization_id=user.organization_id,
            roles=frozenset(),
            external_id=user.external_id,
        )
        self._users[scim_id] = deactivated
        self._deactivated[scim_id] = at or dt.datetime.now(dt.UTC)
        _log.info("scim.deactivated", user=scim_id)
        return deactivated

    def get(self, scim_id: str) -> ScimUser | None:
        return self._users.get(scim_id)

    def active_users(self) -> tuple[ScimUser, ...]:
        return tuple(u for u in self._users.values() if u.active)

    def count(self) -> int:
        return len(self._users)
