"""Ports: the interfaces the domain requires of the outside world.

Each port is a Protocol describing only what the application actually needs, not
the union of what every possible backend can do. Implementations live in the
infrastructure layer and are bound to these protocols at the composition root.
"""

from paimon.domain.ports.health import HealthProbe
from paimon.domain.ports.identity import IdentityProvider

__all__ = ["HealthProbe", "IdentityProvider"]
