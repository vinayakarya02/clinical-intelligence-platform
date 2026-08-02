"""HTTP API for the ingestion service."""

from cip_ingestion.api.app import create_app
from cip_ingestion.api.dependencies import ServiceContainer

__all__ = ["ServiceContainer", "create_app"]
