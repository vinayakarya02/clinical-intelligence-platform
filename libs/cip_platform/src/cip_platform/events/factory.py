"""Choosing an event backbone from settings."""

from __future__ import annotations

from cip_platform.config import PlatformSettings
from cip_platform.events.base import EventBus
from cip_platform.events.kafka import KafkaEventBus
from cip_platform.events.memory import InMemoryEventBus

__all__ = ["EventBackendError", "build_event_bus"]


class EventBackendError(RuntimeError):
    """The configured event backend could not be built."""


def build_event_bus(settings: PlatformSettings) -> EventBus:
    """The event bus this configuration asks for.

    Takes the whole ``PlatformSettings`` rather than a policy object because the event backbone
    is configured by two top-level fields (``events_backend``, ``events_broker_url``) rather than
    by a nested policy — a shape worth keeping visible rather than papering over with a wrapper.
    """
    if settings.events_backend == "memory":
        return InMemoryEventBus()
    if settings.events_backend == "kafka":
        if not settings.events_broker_url.strip():
            raise EventBackendError(
                "events backend is 'kafka' but no events_broker_url is configured"
            )
        return KafkaEventBus(
            settings.events_broker_url,
            client_id=settings.telemetry.service_name,
        )
    raise EventBackendError(f"unknown events backend {settings.events_backend!r}")
