"""``cip_core`` — shared platform primitives.

Everything in this package is consumed by more than one service (today: the ingestion
service and the CLI; later: retrieval, conversational AI, analytics). The rule for what
belongs here is ownership, not convenience: configuration, logging, tenancy, the error
taxonomy, storage, and database connectivity are platform-wide contracts, so a single
implementation lives here rather than being reimplemented per service — the shared-library
requirement from the Phase 0 review (finding A6).
"""

from cip_core.config import Settings, get_settings
from cip_core.errors import CipError
from cip_core.logging import configure_logging, get_logger
from cip_core.tenancy import Role, TenantContext

__all__ = [
    "CipError",
    "Role",
    "Settings",
    "TenantContext",
    "configure_logging",
    "get_logger",
    "get_settings",
]

__version__ = "0.1.0"
