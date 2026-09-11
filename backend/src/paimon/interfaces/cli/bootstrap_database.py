"""Prepare a freshly created database for the workload that will use it.

Everything here is SQL rather than ARM, which is why it is a program and not a
few more lines of Bicep. A Microsoft Entra principal cannot connect to PostgreSQL
until a **role exists for it inside the database**, and creating that role is a
function call that only an administrator may make. The same is true of the vector
extension.

It runs as the administration identity, never as the workload's own
(ADR-0042): the thing a role is created for should not be the thing that creates
it. And it runs inside the virtual network, because since ADR-0038 there is no
route to the database from anywhere else.

Idempotent by construction. It is triggered by hand and will be run again by
anybody unsure whether it was run at all, which is exactly the moment a script
that assumes a clean database does damage.

Usage, as the container apps job runs it::

    uv run python -m paimon.interfaces.cli.bootstrap_database
        --role id-paimon-dev
        --object-id 00000000-0000-0000-0000-000000000000
"""

import argparse
import asyncio
import re
import sys
from collections.abc import Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from paimon.config import get_settings
from paimon.interfaces.api.dependencies import build_database_engine
from paimon.observability import configure_logging, get_logger

logger = get_logger(__name__)

USAGE_ERROR = 2

#: A managed identity's name, which is also the PostgreSQL role name. Checked
#: rather than trusted because it is interpolated into SQL: a role name cannot be
#: a bind parameter, since parameters carry values and this is an identifier.
#: Azure's own rules for a user-assigned identity name are narrower than this.
ROLE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,62}$")

OBJECT_ID = re.compile(r"^[0-9a-fA-F-]{36}$")

#: Extensions the schema needs, created here rather than by the migration.
#:
#: The initial migration does say ``CREATE EXTENSION IF NOT EXISTS vector``, and
#: on a local pgvector image that is enough. On Azure it is two problems: the
#: extension has to be allow-listed on the server first, and creating one is an
#: administrator's privilege that the workload deliberately does not hold. Doing
#: it here makes the migration's statement a no-op instead of a failure, and
#: leaves the workload with no privilege it does not need.
EXTENSIONS = ("vector",)


def quoted(identifier: str) -> str:
    """Quote an identifier for use in SQL.

    Args:
        identifier: An already-validated identifier.

    Returns:
        The identifier, double-quoted, with any internal quote doubled.
    """
    escaped = identifier.replace('"', '""')
    return f'"{escaped}"'


async def role_exists(connection: AsyncConnection, role: str) -> bool:
    """Report whether a PostgreSQL role of this name already exists."""
    found = await connection.scalar(
        text("SELECT 1 FROM pg_roles WHERE rolname = :role"), {"role": role}
    )
    return found is not None


async def bootstrap(
    connection: AsyncConnection, *, role: str, object_id: str, database: str
) -> list[str]:
    """Create the extensions, the role, and the grants the role needs.

    Args:
        connection: An open connection, as an administrator.
        role: Name of the workload's managed identity, which is also its role
            name. It must match exactly, case included: it is the username the
            connection presents, and a mismatch is an authentication failure that
            says nothing whatsoever about names.
        object_id: Object id of that identity. Preferred over the name-only form
            of the function below, which resolves a service principal by display
            name and is ambiguous the moment two of them share one.
        database: Database to grant on.

    Returns:
        What was done, in order, for a caller to print.
    """
    done: list[str] = []

    for extension in EXTENSIONS:
        await connection.execute(text(f"CREATE EXTENSION IF NOT EXISTS {quoted(extension)}"))
        done.append(f"extension {extension} is present")

    if await role_exists(connection, role):
        done.append(f"role {role} already exists")
    else:
        await connection.execute(
            text(
                "SELECT pg_catalog.pgaadauth_create_principal_with_oid("
                ":role, :object_id, 'service', false, false)"
            ),
            {"role": role, "object_id": object_id},
        )
        done.append(f"created role {role}")

    # Both are needed and neither implies the other. The database grant lets the
    # role connect; the schema grant lets it create tables — which PostgreSQL 15
    # stopped giving away, having revoked CREATE on public from PUBLIC. Without
    # the second, the migration authenticates perfectly and then fails on its
    # first CREATE TABLE with a message about a schema rather than a permission.
    await connection.execute(text(f"GRANT ALL ON DATABASE {quoted(database)} TO {quoted(role)}"))
    await connection.execute(text(f"GRANT ALL ON SCHEMA public TO {quoted(role)}"))
    done.append(f"granted {role} the database and the public schema")

    return done


def parse(argv: Sequence[str] | None) -> argparse.Namespace:
    """Parse the command line."""
    parser = argparse.ArgumentParser(
        prog="bootstrap-database",
        description="Create the workload's PostgreSQL role and the extensions it needs.",
    )
    parser.add_argument(
        "--role",
        required=True,
        help="Name of the workload's managed identity, exactly as Azure spells it.",
    )
    parser.add_argument(
        "--object-id",
        required=True,
        help="Object (principal) id of that identity.",
    )
    return parser.parse_args(argv)


async def run(argv: Sequence[str] | None = None) -> int:
    """Bootstrap the database named by the settings.

    Args:
        argv: Command line, or None to read it from the process.

    Returns:
        A process exit code.
    """
    arguments = parse(argv)
    settings = get_settings()
    configure_logging(settings.observability)

    if not ROLE_NAME.match(arguments.role):
        logger.error("invalid_role_name", role=arguments.role)
        return USAGE_ERROR
    if not OBJECT_ID.match(arguments.object_id):
        logger.error("invalid_object_id", object_id=arguments.object_id)
        return USAGE_ERROR

    engine = build_database_engine(settings)
    try:
        async with engine.begin() as connection:
            done = await bootstrap(
                connection,
                role=arguments.role,
                object_id=arguments.object_id,
                database=settings.database.name,
            )
    finally:
        await engine.dispose()

    for line in done:
        logger.info("bootstrap_step", step=line)
    return 0


def main() -> int:
    """Entry point."""
    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())
