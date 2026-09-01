"""The composition root.

This is the single module where concrete adapters are bound to domain ports, and
consequently the only module in the interfaces layer permitted to import from
infrastructure. The rule is enforced by import-linter (ADR-0002).

Routers depend on **use cases and ports**, never on adapters. The providers here
build the object graph; everything downstream receives it already assembled,
which is what keeps the dependency inversion real rather than nominal.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from paimon.application.use_cases import CheckReadiness
from paimon.config import Settings
from paimon.domain.entities import Principal
from paimon.domain.errors import InvalidTokenError
from paimon.domain.ports import HealthProbe, IdentityProvider
from paimon.infrastructure.cache import RedisHealthProbe, build_redis_client
from paimon.infrastructure.identity import build_identity_provider
from paimon.infrastructure.persistence import PostgresHealthProbe, build_engine


@dataclass(frozen=True, slots=True)
class Resources:
    """Process-lifetime objects, built at startup and released at shutdown."""

    settings: Settings
    engine: AsyncEngine
    redis: Redis
    identity_provider: IdentityProvider
    readiness_probes: tuple[HealthProbe, ...]


@asynccontextmanager
async def build_resources(settings: Settings) -> AsyncIterator[Resources]:
    """Construct every long-lived dependency and release it on exit.

    Connections are not opened here. The engine and the Redis client connect
    lazily, so a database that is briefly unavailable delays readiness instead of
    preventing the process from starting — which is what lets an orchestrator
    restart dependencies in any order.

    Args:
        settings: Validated application settings.

    Yields:
        The assembled resources.
    """
    engine = build_engine(settings.database)
    redis = build_redis_client(settings.redis)
    try:
        yield Resources(
            settings=settings,
            engine=engine,
            redis=redis,
            identity_provider=build_identity_provider(settings.auth, settings.environment),
            readiness_probes=(PostgresHealthProbe(engine), RedisHealthProbe(redis)),
        )
    finally:
        await redis.aclose()
        await engine.dispose()


def get_resources(request: Request) -> Resources:
    """Return the resources bound to the running application."""
    resources: Resources = request.app.state.resources
    return resources


ResourcesDep = Annotated[Resources, Depends(get_resources)]


def get_settings_dependency(resources: ResourcesDep) -> Settings:
    """Return the validated application settings."""
    return resources.settings


def get_identity_provider(resources: ResourcesDep) -> IdentityProvider:
    """Return the configured identity adapter, as the port."""
    return resources.identity_provider


def get_check_readiness(resources: ResourcesDep) -> CheckReadiness:
    """Return the readiness use case, wired to the configured probes."""
    return CheckReadiness(resources.readiness_probes)


# auto_error=False so that a missing header produces our own error shape rather
# than FastAPI's, keeping every authentication failure identical to the client.
_bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_principal(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)],
    identity_provider: Annotated[IdentityProvider, Depends(get_identity_provider)],
) -> Principal:
    """Authenticate the caller from the Authorization header.

    Args:
        credentials: Parsed bearer credentials, if the header was present.
        identity_provider: The configured adapter.

    Returns:
        The authenticated caller.

    Raises:
        InvalidTokenError: No usable token was presented, or verification failed.
            Translated to a 401 by the application's exception handler.
    """
    if credentials is None or not credentials.credentials:
        msg = "missing bearer token"
        raise InvalidTokenError(msg)
    return await identity_provider.authenticate(credentials.credentials)


CheckReadinessDep = Annotated[CheckReadiness, Depends(get_check_readiness)]
CurrentPrincipal = Annotated[Principal, Depends(get_current_principal)]
SettingsDep = Annotated[Settings, Depends(get_settings_dependency)]
