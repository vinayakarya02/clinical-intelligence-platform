"""Production platform primitives.

Phase 4. Caching, events, background work, observability, security, MLOps, and
feature flags — as a library the application calls, never a framework that calls it
(docs/design/adr-0013-platform-library-boundary.md).

The dependency rule is one-way and enforced by a test: application code may import
this package; this package never imports application code.
"""

from cip_platform.config import Environment, PlatformSettings, load_platform_settings
from cip_platform.correlation import (
    CorrelationContext,
    current_correlation,
    new_correlation_id,
    set_correlation,
)
from cip_platform.flags import FeatureFlag, FeatureFlags

__all__ = [
    "CorrelationContext",
    "Environment",
    "FeatureFlag",
    "FeatureFlags",
    "PlatformSettings",
    "current_correlation",
    "load_platform_settings",
    "new_correlation_id",
    "set_correlation",
]
