"""Authentication, authorisation, limits, and secrets."""

from cip_platform.security.identity import (
    ApiKeyRecord,
    ApiKeyStore,
    AuthenticationError,
    AuthorizationError,
    Principal,
    Role,
    Scope,
    StaticClaimsVerifier,
    TokenVerifier,
    issue_api_key,
    scopes_for_roles,
)
from cip_platform.security.limits import (
    BudgetDecision,
    RateLimitError,
    SpendBudget,
    TokenBucket,
    TokenBucketLimiter,
)
from cip_platform.security.secrets import (
    EnvironmentSecrets,
    FileSecrets,
    SecretNotFoundError,
    SecretProvider,
    StaticSecrets,
)

__all__ = [
    "ApiKeyRecord",
    "ApiKeyStore",
    "AuthenticationError",
    "AuthorizationError",
    "BudgetDecision",
    "EnvironmentSecrets",
    "FileSecrets",
    "Principal",
    "RateLimitError",
    "Role",
    "Scope",
    "SecretNotFoundError",
    "SecretProvider",
    "SpendBudget",
    "StaticClaimsVerifier",
    "StaticSecrets",
    "TokenBucket",
    "TokenBucketLimiter",
    "TokenVerifier",
    "issue_api_key",
    "scopes_for_roles",
]
