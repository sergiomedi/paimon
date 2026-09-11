"""Authenticating to PostgreSQL without a password.

Under Microsoft Entra there is no database credential: an access token is
presented in the password field. The token is validated **when a connection
opens and never again**, which produces a failure with an unusually nasty shape
— a pool whose existing connections keep working while the next one it opens
cannot authenticate, minutes or hours after a deployment nobody touched.

These tests fix the two things that prevent it: a token is fetched per physical
connection rather than once, and a password is never in the connection string.
"""

from typing import Any

import pytest

from paimon.config import DatabaseSettings
from paimon.infrastructure.persistence import build_engine
from paimon.infrastructure.persistence.engine import POSTGRES_SCOPE


class Token:
    """An access token, as azure-identity returns one."""

    def __init__(self, token: str) -> None:
        self.token = token
        self.expires_on = 0


class Minting:
    """A credential that returns a different token every time it is asked."""

    def __init__(self) -> None:
        self.scopes: list[tuple[str, ...]] = []

    def get_token(self, *scopes: str, **_: Any) -> Token:
        self.scopes.append(scopes)
        return Token(f"token-{len(self.scopes)}")


def settings(**overrides: Any) -> DatabaseSettings:
    values: dict[str, Any] = {
        "host": "db.postgres.database.azure.com",
        "user": "id-paimon-dev",
        "name": "paimon",
        "auth": "entra",
    }
    values.update(overrides)
    return DatabaseSettings(**values)


def connect(engine: Any) -> dict[str, Any]:
    """Fire the hook SQLAlchemy fires when it opens a physical connection."""
    parameters: dict[str, Any] = {}
    dialect = engine.sync_engine.dialect
    # do_connect is a dialect event even when it is registered against the
    # engine, which is where SQLAlchemy routes it to.
    dialect.dispatch.do_connect(dialect, None, (), parameters)
    return parameters


class TestTheConnectionString:
    def test_it_carries_no_password_under_entra(self) -> None:
        # There is nothing to put there, and something that looks like a
        # password in a deployment that has none is a thing somebody will try to
        # rotate.
        assert settings().dsn == (
            "postgresql+asyncpg://id-paimon-dev@db.postgres.database.azure.com:5432/paimon"
        )

    def test_a_password_is_refused_rather_than_ignored(self) -> None:
        # A spare credential is still a credential: unused, unrotated, and one
        # copy-paste away from the deployment that does use one.
        with pytest.raises(ValueError, match="must not be set"):
            settings(password="not-a-real-password")  # noqa: S106

    def test_password_authentication_still_requires_a_password(self) -> None:
        with pytest.raises(ValueError, match="is required"):
            settings(auth="password")


class TestTokensAsPasswords:
    async def test_entra_without_a_credential_is_refused_at_construction(self) -> None:
        # Rather than at the first connection, which is the first request.
        with pytest.raises(ValueError, match="no credential"):
            build_engine(settings())

    async def test_a_token_is_supplied_as_the_password(self) -> None:
        credential = Minting()
        engine = build_engine(settings(), credential)
        try:
            assert connect(engine)["password"] == "token-1"  # noqa: S105
            assert credential.scopes == [(POSTGRES_SCOPE,)]
        finally:
            await engine.dispose()

    async def test_every_new_connection_gets_a_fresh_token(self) -> None:
        # The whole point. A token fetched once and reused is a pool that works
        # until it grows, and then stops — long after anybody was watching.
        credential = Minting()
        engine = build_engine(settings(), credential)
        try:
            assert connect(engine)["password"] == "token-1"  # noqa: S105
            assert connect(engine)["password"] == "token-2"  # noqa: S105
        finally:
            await engine.dispose()

    async def test_the_scope_is_the_database_audience(self) -> None:
        # Not the management scope and not the one the model adapters use. A
        # token for the wrong audience is rejected as a bad password, which says
        # nothing at all about what went wrong.
        assert POSTGRES_SCOPE == "https://ossrdbms-aad.database.windows.net/.default"

    async def test_password_authentication_installs_no_hook(self) -> None:
        engine = build_engine(settings(auth="password", password="local"))  # noqa: S106
        try:
            assert connect(engine) == {}
        finally:
            await engine.dispose()

    async def test_connections_are_recycled_younger_than_a_token(self) -> None:
        engine = build_engine(settings(pool_recycle_seconds=900), Minting())
        try:
            assert engine.pool._recycle == 900  # no public accessor for this
        finally:
            await engine.dispose()
