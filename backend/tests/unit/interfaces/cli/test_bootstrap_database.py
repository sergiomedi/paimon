"""The database bootstrap: what it runs, where, and what it refuses.

These tests drive a fake connection rather than PostgreSQL. What is pinned is the
split — the role is created on the administration database and everything else on
the application's — the sequence within each half, and the two properties that
make the command safe to run by hand: it does not create a role that already
exists, and it does not interpolate an identifier it has not checked.
"""

from typing import Any

import pytest

from paimon.interfaces.cli import bootstrap_database
from paimon.interfaces.cli.bootstrap_database import (
    USAGE_ERROR,
    MissingEntraFunctionsError,
    create_role,
    prepare_database,
    quoted,
)


class FakeConnection:
    """Records the SQL it is given, and answers the two probes."""

    def __init__(self, *, role_exists: bool = False, entra_functions: bool = True) -> None:
        self.statements: list[str] = []
        self.parameters: list[dict[str, Any]] = []
        self._role_exists = role_exists
        self._entra_functions = entra_functions

    async def execute(self, statement: Any, parameters: dict[str, Any] | None = None) -> None:
        """Record a statement."""
        self.statements.append(str(statement))
        self.parameters.append(parameters or {})

    async def scalar(self, statement: Any, parameters: dict[str, Any] | None = None) -> object:
        """Answer whichever probe this is."""
        rendered = str(statement)
        self.statements.append(rendered)
        self.parameters.append(parameters or {})
        if "pg_proc" in rendered:
            return 1 if self._entra_functions else None
        return 1 if self._role_exists else None


async def run_create(connection: FakeConnection) -> str:
    """Create the role with the arguments every test uses."""
    return await create_role(
        connection,  # type: ignore[arg-type]
        role="id-paimon-dev",
        object_id="11111111-2222-3333-4444-555555555555",
    )


async def run_prepare(connection: FakeConnection) -> list[str]:
    """Prepare the application database with the arguments every test uses."""
    return await prepare_database(
        connection,  # type: ignore[arg-type]
        role="id-paimon-dev",
        database="paimon",
    )


@pytest.mark.asyncio
async def test_creates_the_role_by_object_id() -> None:
    """By object id rather than by name: a display name is not unique."""
    connection = FakeConnection(role_exists=False)

    done = await run_create(connection)

    # The call, not the probe that checks the function exists — both name it.
    created = [s for s in connection.statements if "pgaadauth_create_principal_with_oid(:" in s]
    assert len(created) == 1
    assert "'service'" in created[0]
    assert done == "created role id-paimon-dev"


@pytest.mark.asyncio
async def test_does_not_recreate_a_role_that_exists() -> None:
    """It is triggered by hand and will be run twice by anybody who is unsure."""
    connection = FakeConnection(role_exists=True)

    done = await run_create(connection)

    assert not [s for s in connection.statements if "pgaadauth_create_principal_with_oid(:" in s]
    assert "already exists" in done


@pytest.mark.asyncio
async def test_refuses_the_wrong_database_by_name() -> None:
    """The pgaadauth functions live only in the server's own database.

    A connection to the application's database reports them as not existing, with
    a hint about type casts that sends you looking at argument types for a
    function that is not there at all. Checking first turns that into a sentence
    naming the cause.
    """
    connection = FakeConnection(role_exists=False, entra_functions=False)

    with pytest.raises(MissingEntraFunctionsError) as raised:
        await run_create(connection)

    assert "postgres" in str(raised.value)
    assert not [s for s in connection.statements if "pgaadauth_create_principal_with_oid(" in s]


@pytest.mark.asyncio
async def test_prepares_the_extension_before_the_grants() -> None:
    """The extension is an administrator's privilege, and the migration assumes it."""
    connection = FakeConnection()

    await run_prepare(connection)

    assert "CREATE EXTENSION IF NOT EXISTS" in connection.statements[0]
    assert '"vector"' in connection.statements[0]


@pytest.mark.asyncio
async def test_prepare_does_not_touch_roles() -> None:
    """The split is the point: a role is cluster-wide and was created elsewhere."""
    connection = FakeConnection()

    await run_prepare(connection)

    assert not [s for s in connection.statements if "pgaadauth" in s]


@pytest.mark.asyncio
async def test_grants_the_schema_as_well_as_the_database() -> None:
    """PostgreSQL 15 revoked CREATE on public from PUBLIC.

    Without the schema grant the migration authenticates perfectly and then fails
    on its first CREATE TABLE, with a message about a schema rather than about a
    permission. The database grant does not imply it.
    """
    connection = FakeConnection()

    await run_prepare(connection)

    grants = [s for s in connection.statements if s.startswith("GRANT")]
    assert any("ON DATABASE" in grant for grant in grants)
    assert any("ON SCHEMA public" in grant for grant in grants)
    assert all('"id-paimon-dev"' in grant for grant in grants)


@pytest.mark.parametrize(
    "role",
    [
        'paimon"; DROP DATABASE paimon; --',
        "paimon role",
        "1-paimon",
        "",
        "a" * 64,
    ],
)
def test_refuses_a_role_name_it_would_have_to_interpolate(role: str) -> None:
    """A role name cannot be a bind parameter, so it is checked instead.

    Parameters carry values; this is an identifier, and the only defence
    available for an identifier is refusing the ones that are not identifiers.
    """
    assert not bootstrap_database.ROLE_NAME.match(role)


def test_accepts_the_names_azure_actually_produces() -> None:
    """The pattern is narrower than Azure's, but not narrower than its output."""
    assert bootstrap_database.ROLE_NAME.match("id-paimon-dev")
    assert bootstrap_database.ROLE_NAME.match("id-paimon-dbadmin-dev")


def test_quoting_doubles_an_internal_quote() -> None:
    """Belt and braces: the pattern already refuses this, and the quoting holds anyway."""
    assert quoted('a"b') == '"a""b"'


@pytest.mark.asyncio
async def test_a_bad_object_id_is_a_usage_error_not_a_connection_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Checked before anything opens a connection, so the error names the cause."""

    def explode(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("the engine should not have been built")

    monkeypatch.setattr(bootstrap_database, "build_database_engine", explode)
    monkeypatch.setattr(bootstrap_database, "get_settings", _Settings)
    monkeypatch.setattr(bootstrap_database, "configure_logging", lambda _settings: None)

    code = await bootstrap_database.run(["--role", "id-paimon-dev", "--object-id", "nonsense"])

    assert code == USAGE_ERROR


class _Settings:
    """The two attributes run() reads before it validates its arguments."""

    observability = object()
