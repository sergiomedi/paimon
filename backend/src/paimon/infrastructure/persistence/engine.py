"""SQLAlchemy engine construction.

Two ways to authenticate, and the second one is the reason this file is longer
than a single ``create_async_engine`` call.

With a password, the credential lives in the connection string and the pool
reuses it forever. With Microsoft Entra there is no password: an access token is
presented in its place, and the token **is checked only when a connection
opens**. A pooled connection therefore keeps working long after the token that
opened it has expired, while the next connection the pool decides to open fails
to authenticate — minutes or hours after the deployment, on a code path nobody
changed. Baking a token into a connection string is the shape of that bug.

So the token is fetched per connection, in a hook, and connections are recycled
young enough that they stay comfortably inside a token's life.
"""

from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from paimon.config import DatabaseSettings
from paimon.infrastructure.azure.credentials import TokenProvider

POSTGRES_SCOPE = "https://ossrdbms-aad.database.windows.net/.default"
"""Audience a token must carry to be accepted by Azure Database for PostgreSQL.

Not the generic management scope, and not the one the other Azure adapters use.
A token for the wrong audience is refused as a bad password, which is a long way
from what actually went wrong.
"""


def build_engine(
    settings: DatabaseSettings, credential: TokenProvider | None = None
) -> AsyncEngine:
    """Build the async engine used by HTTP request handling.

    The agent runtime gets its own engine, sized from ``agent_pool_size``.
    Keeping the pools separate is the point (ADR-0007): agent graphs hold
    connections for minutes, and sharing one pool lets a few concurrent runs
    starve the API.

    Args:
        settings: Connection and pool configuration.
        credential: Mints Entra tokens. Required when ``settings.auth`` is
            ``entra`` and unused otherwise.

    Returns:
        An engine that must be disposed of during shutdown.

    Raises:
        ValueError: If Entra authentication is configured with no credential to
            obtain tokens from.
    """
    if settings.auth == "entra" and credential is None:
        msg = "database.auth is 'entra' but no credential was supplied to mint tokens"
        raise ValueError(msg)

    engine = create_async_engine(
        settings.dsn,
        pool_size=settings.pool_size,
        max_overflow=settings.max_overflow,
        pool_timeout=settings.pool_timeout_seconds,
        # Verifies a pooled connection before handing it out. Costs a round trip
        # and removes the class of failure where a connection killed by a
        # database restart or an idle-timeout proxy surfaces as a request error.
        pool_pre_ping=True,
        # Only meaningful under Entra, where it keeps pooled connections younger
        # than the token that opened them. Harmless otherwise.
        pool_recycle=settings.pool_recycle_seconds if settings.auth == "entra" else -1,
        echo=settings.echo_sql,
    )

    if settings.auth == "entra":
        assert credential is not None  # noqa: S101  narrowed by the check above
        _authenticate_with_tokens(engine, credential)

    return engine


def _authenticate_with_tokens(engine: AsyncEngine, credential: TokenProvider) -> None:
    """Supply a fresh token as the password on every new connection.

    ``do_connect`` fires once per **physical** connection, not per checkout, so
    this costs a token fetch when the pool grows or recycles and nothing at all
    on the hot path. The credential caches and refreshes internally, so most of
    those fetches never leave the process.
    """

    @event.listens_for(engine.sync_engine, "do_connect")
    def _provide_token(_dialect: Any, _record: Any, _args: Any, parameters: dict[str, Any]) -> None:
        parameters["password"] = credential.get_token(POSTGRES_SCOPE).token
