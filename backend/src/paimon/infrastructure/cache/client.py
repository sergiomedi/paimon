"""Redis client construction."""

from redis.asyncio import Redis

from paimon.config import RedisSettings


def build_redis_client(settings: RedisSettings) -> Redis:
    """Build the async Redis client.

    Args:
        settings: Connection configuration.

    Returns:
        A client that must be closed during shutdown.
    """
    return Redis.from_url(
        settings.url,
        socket_timeout=settings.socket_timeout_seconds,
        socket_connect_timeout=settings.socket_timeout_seconds,
        decode_responses=True,
    )
