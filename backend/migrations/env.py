"""Alembic environment.

Runs migrations against the same database the service connects to, using the
same settings object, so there is no second definition of where the database is
or who connects to it.
"""

import asyncio

from alembic import context
from sqlalchemy import Connection, pool
from sqlalchemy.ext.asyncio import AsyncEngine, async_engine_from_config

from paimon.config import get_settings
from paimon.infrastructure.azure import build_token_provider
from paimon.infrastructure.persistence.engine import build_engine
from paimon.infrastructure.persistence.models import Base

config = context.config
target_metadata = Base.metadata


def _url_override() -> str | None:
    """An explicitly supplied URL, or ``None`` to use the application's settings.

    Two sources, in order: a URL set programmatically (how the integration tests
    migrate a throwaway database), then ``-x db_url=...`` on the command line.
    Neither is the normal path, and it is why there is no database URL written
    into alembic.ini.
    """
    programmatic = config.attributes.get("db_url")
    if programmatic:
        return str(programmatic)
    override = context.get_x_argument(as_dictionary=True).get("db_url")
    if override:
        return str(override)
    return None


def _database_url() -> str:
    """The URL to migrate, for the offline mode that only renders SQL."""
    return _url_override() or get_settings().database.dsn


def _engine() -> AsyncEngine:
    """The engine to migrate with.

    An explicit URL is honoured exactly as written, on a null pool: a caller who
    passed a URL meant that URL, and the integration tests pass one pointing at a
    throwaway database.

    Everything else goes through the application's own engine builder rather than
    through a URL, because since ADR-0038 a URL is not enough to connect. Under
    Microsoft Entra authentication the credential is a token presented in the
    password field and fetched per connection, and only ``build_engine`` installs
    the hook that does it. Handing a DSN to SQLAlchemy here would work on a laptop
    and fail in every deployed environment — which is the worst place for the
    difference to show up, since the deployed database is the only one a
    migration cannot be rehearsed against.
    """
    override = _url_override()
    if override is not None:
        section: dict[str, str] = config.get_section(config.config_ini_section, {})
        section["sqlalchemy.url"] = override
        return async_engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)

    database = get_settings().database
    # build_token_provider imports azure-identity lazily and says so when it is
    # missing, so a local migration never needs the extra installed.
    provider = build_token_provider() if database.auth == "entra" else None
    return build_engine(database, provider)


def include_object(
    obj: object,  # noqa: ARG001  part of Alembic's callback signature
    name: str | None,
    type_: str,
    reflected: bool,  # noqa: ARG001, FBT001  Alembic's callback signature
    compare_to: object,  # noqa: ARG001
) -> bool:
    """Keep autogenerate from proposing changes to things it does not own."""
    return not (type_ == "table" and name in {"alembic_version"})


def run_migrations_offline() -> None:
    """Emit SQL without connecting, for review or for a DBA to apply."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Run the migrations on an open connection."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_object=include_object,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Connect asynchronously and run the migrations."""
    engine = _engine()
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


def run_migrations_online() -> None:
    """Entry point for online migrations."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
