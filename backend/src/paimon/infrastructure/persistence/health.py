"""Readiness probe for PostgreSQL."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


class PostgresHealthProbe:
    """Checks that the database accepts connections and answers queries."""

    def __init__(self, engine: AsyncEngine, component: str = "postgresql") -> None:
        """Initialise the probe.

        Args:
            engine: Engine whose pool is exercised by the check.
            component: Name reported in readiness output.
        """
        self._engine = engine
        self._component = component

    @property
    def component(self) -> str:
        """Name of the component this probe covers."""
        return self._component

    async def check(self) -> None:
        """Acquire a connection from the pool and run a trivial query.

        Executing a statement rather than only connecting is deliberate: a
        database that accepts connections but refuses queries — read-only after a
        failover, out of disk — is not ready, and a connect-only check would call
        it healthy.
        """
        async with self._engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
