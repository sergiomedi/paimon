"""SQLAlchemy engine construction."""

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from paimon.config import DatabaseSettings


def build_engine(settings: DatabaseSettings) -> AsyncEngine:
    """Build the async engine used by HTTP request handling.

    The agent runtime gets its own engine, sized from ``agent_pool_size``, when
    the agent runtime itself arrives in Phase 3. Keeping the pools separate is
    the point (ADR-0007): agent graphs hold connections for minutes, and sharing
    one pool lets a few concurrent runs starve the API.

    Args:
        settings: Connection and pool configuration.

    Returns:
        An engine that must be disposed of during shutdown.
    """
    return create_async_engine(
        settings.dsn,
        pool_size=settings.pool_size,
        max_overflow=settings.max_overflow,
        pool_timeout=settings.pool_timeout_seconds,
        # Verifies a pooled connection before handing it out. Costs a round trip
        # and removes the class of failure where a connection killed by a
        # database restart or an idle-timeout proxy surfaces as a request error.
        pool_pre_ping=True,
        echo=settings.echo_sql,
    )
