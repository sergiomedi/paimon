"""Tests for Azure authentication."""

import time
from typing import Any

import pytest

from paimon.infrastructure.azure import ApiKeyCredential, EntraCredential, build_credential
from paimon.infrastructure.azure.credentials import AzureAuthenticationError


class StubToken:
    def __init__(self, token: str, expires_on: int) -> None:
        self.token = token
        self.expires_on = expires_on


class StubTokenCredential:
    """Stands in for an azure-identity credential."""

    def __init__(self, lifetime: float = 3600.0, *, fail: bool = False) -> None:
        self.calls = 0
        self._lifetime = lifetime
        self._fail = fail

    def get_token(self, *scopes: str, **kwargs: Any) -> StubToken:
        self.calls += 1
        if self._fail:
            msg = "no managed identity available"
            raise RuntimeError(msg)
        return StubToken(f"token-{self.calls}", int(time.time() + self._lifetime))


class TestApiKey:
    async def test_the_key_goes_in_the_expected_header(self) -> None:
        assert await ApiKeyCredential("secret").headers() == {"api-key": "secret"}

    async def test_the_header_is_configurable(self) -> None:
        credential = ApiKeyCredential("secret", header="Authorization")
        assert await credential.headers() == {"Authorization": "secret"}


class TestEntra:
    async def test_it_returns_a_bearer_header(self) -> None:
        credential = EntraCredential(StubTokenCredential(), "scope/.default")
        assert await credential.headers() == {"Authorization": "Bearer token-1"}

    async def test_a_valid_token_is_reused(self) -> None:
        """The request path must not pay for a token fetch it does not need."""
        inner = StubTokenCredential()
        credential = EntraCredential(inner, "scope/.default")

        await credential.headers()
        await credential.headers()

        assert inner.calls == 1

    async def test_a_token_close_to_expiry_is_refreshed(self) -> None:
        """Refreshing early means a request never leaves with a token that dies
        in flight."""
        inner = StubTokenCredential(lifetime=60.0)
        credential = EntraCredential(inner, "scope/.default")

        first = await credential.headers()
        second = await credential.headers()

        assert inner.calls == 2
        assert first != second

    async def test_a_failure_is_reported_as_an_authentication_error(self) -> None:
        credential = EntraCredential(StubTokenCredential(fail=True), "scope/.default")
        with pytest.raises(AzureAuthenticationError, match="could not obtain an Entra ID token"):
            await credential.headers()


class TestSelection:
    async def test_a_configured_key_wins(self) -> None:
        credential = build_credential("secret", "scope/.default")
        assert await credential.headers() == {"api-key": "secret"}

    def test_no_key_falls_back_to_entra(self) -> None:
        """A deployment removes its keys by deleting them from configuration, not
        by also remembering to flip a mode."""
        credential = build_credential(None, "scope/.default")
        assert isinstance(credential, EntraCredential)
