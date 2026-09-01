"""Readiness probe for Redis."""

from redis.asyncio import Redis

from paimon.domain.errors import HealthCheckFailedError


class RedisHealthProbe:
    """Checks that Redis is reachable and responding."""

    def __init__(self, client: Redis, component: str = "redis") -> None:
        """Initialise the probe.

        Args:
            client: Client used for the check.
            component: Name reported in readiness output.
        """
        self._client = client
        self._component = component

    @property
    def component(self) -> str:
        """Name of the component this probe covers."""
        return self._component

    async def check(self) -> None:
        """Send a PING and require a response.

        Raises:
            HealthCheckFailedError: Redis answered, but not with a pong.
        """
        if not await self._client.ping():
            msg = "redis did not answer PING"
            raise HealthCheckFailedError(msg)
