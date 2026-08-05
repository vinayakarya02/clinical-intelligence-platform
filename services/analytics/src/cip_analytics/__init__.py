"""Analytics warehouse and self-service reporting.

Phase 7. A dimensional warehouse fed by a de-identifying ETL, a declarative metric layer, a
template-only query surface with statistical disclosure control, four dashboard categories, and
scheduled reports.

Distinct from Phase 6's real-time dashboards, which answer "what is happening now" over a
sliding window and keep no history. This answers "what happened, and how does it compare" over a
warehouse loaded on a schedule. See docs/architecture/11-analytics-warehouse.md.

**The warehouse holds no direct identifiers**, because the ETL removes them on the way in and
nothing else writes to it (docs/design/adr-0033-deidentify-at-load.md).
"""

from cip_analytics.domain import (
    ANALYTICS_ELEVATED,
    ANALYTICS_GOVERNANCE,
    ANALYTICS_READ,
    AnalyticsError,
    AnalyticsPrincipal,
    DisclosurePolicy,
    Freshness,
    MeasureKind,
    QueryError,
    SchemaError,
    SuppressionError,
)
from cip_analytics.warehouse import Warehouse, WarehouseSchema, default_schema

__all__ = [
    "ANALYTICS_ELEVATED",
    "ANALYTICS_GOVERNANCE",
    "ANALYTICS_READ",
    "AnalyticsError",
    "AnalyticsPrincipal",
    "DisclosurePolicy",
    "Freshness",
    "MeasureKind",
    "QueryError",
    "SchemaError",
    "SuppressionError",
    "Warehouse",
    "WarehouseSchema",
    "default_schema",
]
