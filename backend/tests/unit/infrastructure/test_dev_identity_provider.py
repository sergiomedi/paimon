"""Tests for the locally signed identity adapter."""

from datetime import timedelta

import jwt
import pytest
from pydantic import SecretStr

from paimon.config import AuthSettings, Environment
from paimon.domain.errors import InvalidTokenError
from paimon.infrastructure.identity import DevIdentityProvider, build_identity_provider

KEY = "unit-test-key-padded-to-thirty-two-bytes"


@pytest.fixture
def provider() -> DevIdentityProvider:
    return DevIdentityProvider(signing_key=KEY)


class TestRoundTrip:
    async def test_an_issued_token_authenticates(self, provider: DevIdentityProvider) -> None:
        token = provider.issue(
            subject="user-1",
            tenant_id="tenant-1",
            display_name="Ada",
            roles=frozenset({"reader", "admin"}),
        )
        principal = await provider.authenticate(token)

        assert principal.subject == "user-1"
        assert principal.tenant_id == "tenant-1"
        assert principal.display_name == "Ada"
        assert principal.roles == frozenset({"reader", "admin"})


class TestRejection:
    async def test_an_expired_token_is_rejected(self, provider: DevIdentityProvider) -> None:
        token = provider.issue(
            subject="user-1", tenant_id="tenant-1", expires_in=timedelta(seconds=-120)
        )
        with pytest.raises(InvalidTokenError):
            await provider.authenticate(token)

    async def test_a_token_signed_with_another_key_is_rejected(
        self, provider: DevIdentityProvider
    ) -> None:
        other = DevIdentityProvider(signing_key="a-different-key-also-thirty-two-plus")
        token = other.issue(subject="user-1", tenant_id="tenant-1")
        with pytest.raises(InvalidTokenError):
            await provider.authenticate(token)

    async def test_a_token_for_another_audience_is_rejected(
        self, provider: DevIdentityProvider
    ) -> None:
        other = DevIdentityProvider(signing_key=KEY, audience="some-other-service")
        token = other.issue(subject="user-1", tenant_id="tenant-1")
        with pytest.raises(InvalidTokenError):
            await provider.authenticate(token)

    async def test_garbage_is_rejected(self, provider: DevIdentityProvider) -> None:
        with pytest.raises(InvalidTokenError):
            await provider.authenticate("not-a-token")

    async def test_a_token_without_a_tenant_claim_is_rejected(
        self, provider: DevIdentityProvider
    ) -> None:
        """Tenant is the isolation boundary; a token without one cannot be trusted."""
        token = jwt.encode(
            {
                "oid": "user-1",
                "iss": "paimon-dev",
                "aud": "paimon-local",
                "iat": 1_700_000_000,
                "exp": 4_100_000_000,
            },
            KEY,
            algorithm="HS256",
        )
        with pytest.raises(InvalidTokenError, match="missing the 'tid' claim"):
            await provider.authenticate(token)

    async def test_an_unsigned_token_is_rejected(self, provider: DevIdentityProvider) -> None:
        """The 'none' algorithm attack, asserted rather than assumed."""
        token = jwt.encode({"oid": "user-1", "tid": "tenant-1"}, key="", algorithm="none")
        with pytest.raises(InvalidTokenError):
            await provider.authenticate(token)


class TestFactoryGuards:
    @pytest.mark.parametrize("environment", [Environment.STAGING, Environment.PRODUCTION])
    def test_the_dev_signer_is_refused_in_deployed_environments(
        self, environment: Environment
    ) -> None:
        """A second guard behind the settings validator: an authentication bypass
        is worth checking twice."""
        settings = AuthSettings(provider="dev", dev_signing_key=SecretStr(KEY))
        with pytest.raises(ValueError, match="not allowed in"):
            build_identity_provider(settings, environment)

    @pytest.mark.parametrize("environment", [Environment.LOCAL, Environment.TEST])
    def test_the_dev_signer_is_allowed_locally(self, environment: Environment) -> None:
        settings = AuthSettings(provider="dev", dev_signing_key=SecretStr(KEY))
        assert isinstance(build_identity_provider(settings, environment), DevIdentityProvider)

    def test_entra_is_selected_when_configured(self) -> None:
        settings = AuthSettings(provider="entra", tenant_id="tenant", audience="api://paimon")
        provider = build_identity_provider(settings, Environment.PRODUCTION)
        assert type(provider).__name__ == "EntraIdentityProvider"
