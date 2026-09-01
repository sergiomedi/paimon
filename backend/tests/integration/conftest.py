"""Fixtures for tests that require real PostgreSQL and Redis.

Start them with:

    docker compose -f docker/compose.yaml up -d

When the services are unreachable these tests skip, so a contributor without
Docker running still gets a useful local run. In CI that would silently turn a
broken adapter into a green build, so CI sets ``PAIMON_REQUIRE_INTEGRATION=1``
and the same condition fails instead of skipping.
"""

import os
from collections.abc import AsyncIterator

import pytest
from pydantic import SecretStr
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from paimon.config import DatabaseSettings, RedisSettings
from paimon.infrastructure.cache import build_redis_client
from paimon.infrastructure.persistence import build_engine

REQUIRE_INTEGRATION = os.environ.get("PAIMON_REQUIRE_INTEGRATION") == "1"


def unavailable(service: str, error: Exception) -> None:
    """Skip locally, fail in CI."""
    message = f"{service} is not reachable: {error}"
    if REQUIRE_INTEGRATION:
        pytest.fail(f"{message} (PAIMON_REQUIRE_INTEGRATION=1)")
    pytest.skip(f"{message} — start it with docker compose -f docker/compose.yaml up -d")


@pytest.fixture(scope="session")
def database_settings() -> DatabaseSettings:
    return DatabaseSettings(
        host=os.environ.get("PAIMON_DATABASE__HOST", "localhost"),
        port=int(os.environ.get("PAIMON_DATABASE__PORT", "5432")),
        user=os.environ.get("PAIMON_DATABASE__USER", "paimon"),
        password=SecretStr(os.environ.get("PAIMON_DATABASE__PASSWORD", "paimon")),
        name=os.environ.get("PAIMON_DATABASE__NAME", "paimon"),
        pool_size=2,
        max_overflow=0,
    )


@pytest.fixture(scope="session")
def redis_settings() -> RedisSettings:
    return RedisSettings(
        host=os.environ.get("PAIMON_REDIS__HOST", "localhost"),
        port=int(os.environ.get("PAIMON_REDIS__PORT", "6379")),
    )


@pytest.fixture
async def engine(database_settings: DatabaseSettings) -> AsyncIterator[AsyncEngine]:
    created = build_engine(database_settings)
    try:
        async with created.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception as error:  # noqa: BLE001  reported as a skip or a failure
        await created.dispose()
        unavailable("PostgreSQL", error)
    try:
        yield created
    finally:
        await created.dispose()


@pytest.fixture
async def redis(redis_settings: RedisSettings) -> AsyncIterator[Redis]:
    client = build_redis_client(redis_settings)
    try:
        await client.ping()
    except Exception as error:  # noqa: BLE001  reported as a skip or a failure
        await client.aclose()
        unavailable("Redis", error)
    try:
        yield client
    finally:
        await client.aclose()
