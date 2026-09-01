"""Shared fixtures."""

from collections.abc import AsyncIterator

import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from paimon.config import (
    AuthSettings,
    DatabaseSettings,
    Environment,
    ObservabilitySettings,
    RedisSettings,
    Settings,
)
from paimon.infrastructure.identity import DevIdentityProvider
from paimon.interfaces.api import create_app

DEV_SIGNING_KEY = "test-signing-key-padded-to-thirty-two-bytes"


@pytest.fixture
def settings() -> Settings:
    """Settings for a test process, built explicitly rather than from the environment."""
    return Settings(
        _env_file=None,
        environment=Environment.TEST,
        database=DatabaseSettings(
            host="localhost",
            user="paimon",
            password=SecretStr("test"),
            name="paimon_test",
        ),
        redis=RedisSettings(host="localhost"),
        auth=AuthSettings(provider="dev", dev_signing_key=SecretStr(DEV_SIGNING_KEY)),
        observability=ObservabilitySettings(log_format="console", log_level="WARNING"),
    )


@pytest.fixture
def dev_identity_provider() -> DevIdentityProvider:
    """Signer matching the configuration used by the test application."""
    return DevIdentityProvider(signing_key=DEV_SIGNING_KEY)


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    """The application, not yet started."""
    return create_app(settings)


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """An HTTP client bound to the running application.

    LifespanManager runs startup and shutdown, so the resources the endpoints
    depend on are built the same way they are in a real process. No connection is
    opened: the engine and Redis client connect lazily.
    """
    async with (
        LifespanManager(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as http_client,
    ):
        yield http_client
