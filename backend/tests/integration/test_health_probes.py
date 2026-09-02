"""Integration tests for the readiness probes against real services.

These are the paths the unit suite cannot cover: a probe is only meaningful if
it behaves correctly against the actual database and cache.
"""

import pytest
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from paimon.application.use_cases import CheckReadiness
from paimon.config import DatabaseSettings
from paimon.infrastructure.cache import RedisHealthProbe
from paimon.infrastructure.persistence import PostgresHealthProbe

pytestmark = pytest.mark.integration


class TestPostgres:
    async def test_the_probe_passes_against_a_running_database(self, engine: AsyncEngine) -> None:
        await PostgresHealthProbe(engine).check()

    async def test_the_probe_fails_against_a_closed_port(
        self, database_settings: DatabaseSettings
    ) -> None:
        unreachable = create_async_engine(
            database_settings.dsn.replace(f":{database_settings.port}/", ":1/"),
            pool_pre_ping=False,
        )
        try:
            with pytest.raises(Exception, match=r"[Cc]onnect"):
                await PostgresHealthProbe(unreachable).check()
        finally:
            await unreachable.dispose()

    async def test_pgvector_is_available(self, engine: AsyncEngine) -> None:
        """Phase 2's local retrieval adapter depends on this extension, so the
        image is verified now rather than discovered to be wrong later."""
        async with engine.begin() as connection:
            await connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        async with engine.connect() as connection:
            result = await connection.execute(
                text("SELECT extname FROM pg_extension WHERE extname = 'vector'")
            )
            assert result.scalar_one() == "vector"


class TestRedis:
    async def test_the_probe_passes_against_a_running_cache(self, redis: Redis) -> None:
        await RedisHealthProbe(redis).check()


class TestReadinessAgainstRealServices:
    async def test_the_report_is_ready_when_both_are_up(
        self, engine: AsyncEngine, redis: Redis
    ) -> None:
        report = await CheckReadiness([PostgresHealthProbe(engine), RedisHealthProbe(redis)])()
        assert report.is_ready, report.components
        assert all(component.latency_ms > 0 for component in report.components)
