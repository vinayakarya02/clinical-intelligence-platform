"""Gateway: the composition root.

Wires the platform library to the application layers. This is the only place that knows about
both, which is what keeps the dependency direction one-way
(docs/design/adr-0013-platform-library-boundary.md).
"""

from cip_gateway.health import DependencyCheck, HealthReport, HealthService
from cip_gateway.middleware import GatewayContext, GatewayGuards, problem_response

__all__ = [
    "DependencyCheck",
    "GatewayContext",
    "GatewayGuards",
    "HealthReport",
    "HealthService",
    "problem_response",
]
