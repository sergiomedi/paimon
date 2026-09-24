"""Fixtures for tests that require real PostgreSQL and Redis.

Start them with:

    docker compose -f docker/compose.yaml up -d

When the services are unreachable these tests skip, so a contributor without
Docker running still gets a useful local run. In CI that would silently turn a
broken adapter into a green build, so CI sets ``PAIMON_TEST_REQUIRE_INTEGRATION=1``
and the same condition fails instead of skipping.
"""

import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from pydantic import SecretStr
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine

from paimon.config import DatabaseSettings, RedisSettings
from paimon.infrastructure.cache import build_redis_client
from paimon.infrastructure.persistence import build_engine

REQUIRE_INTEGRATION = os.environ.get("PAIMON_TEST_REQUIRE_INTEGRATION") == "1"
BACKEND_ROOT = Path(__file__).resolve().parents[2]

#: Integration tests truncate tables. They may only do that to a database whose
#: name says it exists to be truncated.
TEST_DATABASE_SUFFIX = "_test"


class WrongDatabaseError(RuntimeError):
    """The integration suite was pointed at a database it must not empty."""


def require_disposable(name: str) -> None:
    """Refuse to run against a database that is not obviously disposable.

    These tests `TRUNCATE TABLE agent_runs, agent_memories` and
    `TRUNCATE TABLE chunks, documents`. Run against a working database that is
    exactly what they do, and it has happened twice: once to a corpus in the
    middle of a benchmark, producing a plausible and entirely false result, and
    once to the `agent_runs` rows holding the withdrawn drafts of a finished
    measurement, discovered only when somebody went looking for them.

    The first incident was answered by guarding one more table at the point of
    use. That did not generalise, because the next destructive test truncated a
    different table. So the check moved here, to the name of the database
    itself: whatever a test truncates, it can only truncate it somewhere
    disposable.

    Raises:
        WrongDatabase: If the name does not end in ``_test``.
    """
    if not name.endswith(TEST_DATABASE_SUFFIX):
        msg = (
            f"refusing to run integration tests against database '{name}': "
            f"they TRUNCATE tables, so the name must end in "
            f"'{TEST_DATABASE_SUFFIX}'.\n"
            "    Create one and point the suite at it:\n"
            '      docker exec paimon-postgres-1 psql -U paimon -c "CREATE DATABASE paimon_test"\n'
            "      PAIMON_DATABASE__NAME=paimon_test uv run pytest tests/integration\n"
            "    scripts/check.sh does this for you."
        )
        raise WrongDatabaseError(msg)


def unavailable(service: str, error: Exception) -> None:
    """Skip locally, fail in CI."""
    message = f"{service} is not reachable: {error}"
    if REQUIRE_INTEGRATION:
        pytest.fail(f"{message} (PAIMON_TEST_REQUIRE_INTEGRATION=1)")
    pytest.skip(f"{message} — start it with docker compose -f docker/compose.yaml up -d")


@pytest.fixture(scope="session")
def database_settings() -> DatabaseSettings:
    """Where the integration tests run, once it is proved safe to empty it.

    Checked here rather than in each destructive test: this fixture is upstream
    of every one of them, so nothing can truncate anything without passing it.
    """
    name = os.environ.get("PAIMON_DATABASE__NAME", "paimon_test")
    require_disposable(name)
    return DatabaseSettings(
        host=os.environ.get("PAIMON_DATABASE__HOST", "localhost"),
        port=int(os.environ.get("PAIMON_DATABASE__PORT", "5432")),
        user=os.environ.get("PAIMON_DATABASE__USER", "paimon"),
        password=SecretStr(os.environ.get("PAIMON_DATABASE__PASSWORD", "paimon")),
        name=name,
        pool_size=2,
        max_overflow=0,
    )


@pytest.fixture(scope="session")
def redis_settings() -> RedisSettings:
    return RedisSettings(
        host=os.environ.get("PAIMON_REDIS__HOST", "localhost"),
        port=int(os.environ.get("PAIMON_REDIS__PORT", "6379")),
    )


@pytest.fixture(scope="session")
def migrated_database(database_settings: DatabaseSettings) -> None:
    """Bring the schema to head before anything touches it.

    Runs the real migrations rather than metadata.create_all, so the tests
    exercise the same path a deployment takes — a schema created from metadata
    can differ from the one migrations produce, and the difference only surfaces
    in production.

    Synchronous on purpose: Alembic's env.py opens its own event loop, which it
    cannot do from inside a running one.
    """
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.attributes["db_url"] = database_settings.dsn
    try:
        command.upgrade(config, "head")
    except (OperationalError, InterfaceError, OSError) as error:
        # Only connection failures mean "no database here". A broken migration
        # must surface as a broken migration: reporting it as an unreachable
        # service sends the reader to start Docker instead of reading the error.
        unavailable("PostgreSQL", error)


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
