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

#: The database the Entra principal functions live in.
#:
#: Not the application's. Azure installs ``pgaadauth_create_principal`` and its
#: relatives **only in the server's own ``postgres`` database**, and a connection
#: to any other database reports them as simply not existing — an
#: ``UndefinedFunctionError`` whose hint suggests adding type casts, which sends
#: you looking at argument types for a function that is not there at all.
#:
#: A role, once created, is cluster-wide. So the role is created on a connection
#: to this database and everything else happens on the application's own, which
#: is where an extension and a schema grant have to happen because both belong to
#: one database rather than to the server.
ADMINISTRATION_DATABASE = "postgres"

WRONG_DATABASE = (
    "the pgaadauth functions are not available on this connection. They exist "
    f"only in the server's {ADMINISTRATION_DATABASE!r} database, so the role has "
    "to be created there — see ADMINISTRATION_DATABASE in this module."
)


class MissingEntraFunctionsError(RuntimeError):
    """Azure's Entra principal functions are not reachable from this connection."""


def quoted(identifier: str) -> str:
    """Quote an identifier for use in SQL.

    Args:
        identifier: An already-validated identifier.

    Returns:
        The identifier, double-quoted, with any internal quote doubled.
    """
    escaped = identifier.replace('"', '""')
    return f'"{escaped}"'


async def entra_functions_present(connection: AsyncConnection) -> bool:
    """Report whether Azure's Entra principal functions exist on this connection.

    Checked before calling one rather than after failing to, so that the error
    names the cause — the wrong database — instead of arriving as a missing
    function and a misleading hint about type casts.
    """
    found = await connection.scalar(
        text(
            "SELECT 1 FROM pg_catalog.pg_proc WHERE proname = 'pgaadauth_create_principal_with_oid'"
        )
    )
    return found is not None


async def role_exists(connection: AsyncConnection, role: str) -> bool:
    """Report whether a PostgreSQL role of this name already exists."""
    found = await connection.scalar(
        text("SELECT 1 FROM pg_roles WHERE rolname = :role"), {"role": role}
    )
    return found is not None


async def create_role(connection: AsyncConnection, *, role: str, object_id: str) -> str:
    """Create the workload's role, on a connection to the administration database.

    Args:
        connection: An open connection **to the ``postgres`` database**, as a
            Microsoft Entra administrator.
        role: Name of the workload's managed identity, which is also its role
            name. It must match exactly, case included: it is the username the
            connection presents, and a mismatch is an authentication failure that
            says nothing whatsoever about names.
        object_id: Object id of that identity. Preferred over the name-only form
            of the function, which resolves a service principal by display name
            and is ambiguous the moment two of them share one.

    Returns:
        One line describing what happened.

    Raises:
        MissingEntraFunctionsError: If the ``pgaadauth`` functions are not reachable.
    """
    if await role_exists(connection, role):
        return f"role {role} already exists"

    if not await entra_functions_present(connection):
        raise MissingEntraFunctionsError(WRONG_DATABASE)

    await connection.execute(
        text(
            "SELECT pg_catalog.pgaadauth_create_principal_with_oid("
            ":role, :object_id, 'service', false, false)"
        ),
        {"role": role, "object_id": object_id},
    )
    return f"created role {role}"


async def prepare_database(connection: AsyncConnection, *, role: str, database: str) -> list[str]:
    """Create the extensions and the grants, on the application's own database.

    A role is cluster-wide, so the one created above already exists here. An
    extension and a schema grant are not: both belong to one database, and doing
    them on the administration connection would prepare the wrong database
    perfectly.

    Args:
        connection: An open connection to the application's database.
        role: The workload's role name.
        database: The database being granted.

    Returns:
        What was done, in order, for a caller to print.
    """
    done: list[str] = []

    for extension in EXTENSIONS:
        await connection.execute(text(f"CREATE EXTENSION IF NOT EXISTS {quoted(extension)}"))
        done.append(f"extension {extension} is present")

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

    done: list[str] = []

    # Two connections, to two databases, because the work splits that way and
    # nothing can paper over it: the principal functions exist only in the
    # server's own database, and an extension and a schema grant exist only in
    # the application's.
    administration = settings.model_copy(
        update={"database": settings.database.model_copy(update={"name": ADMINISTRATION_DATABASE})}
    )
    engine = build_database_engine(administration)
    try:
        async with engine.begin() as connection:
            done.append(
                await create_role(connection, role=arguments.role, object_id=arguments.object_id)
            )
    except MissingEntraFunctionsError as error:
        logger.error("bootstrap_failed", reason=str(error))
        return 1
    finally:
        await engine.dispose()

    engine = build_database_engine(settings)
    try:
        async with engine.begin() as connection:
            done.extend(
                await prepare_database(
                    connection, role=arguments.role, database=settings.database.name
                )
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
