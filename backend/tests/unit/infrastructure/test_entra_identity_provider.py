"""Tests for the Entra ID adapter.

The tenant's signing keys are replaced by a locally generated RSA pair, so the
adapter's own verification logic — algorithm, audience, issuer, time claims and
the distinction between a bad token and an unreachable provider — is exercised
without a network call or a tenant.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from paimon.domain.errors import IdentityProviderUnavailableError, InvalidTokenError
from paimon.infrastructure.identity import EntraIdentityProvider

TENANT = "tenant-abc"
AUDIENCE = "api://paimon"
ISSUER = f"https://login.microsoftonline.com/{TENANT}/v2.0"


@pytest.fixture(scope="module")
def key_pair() -> tuple[str, str]:
    """A private and public PEM pair standing in for the tenant's signing key."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = (
        private_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return private_pem, public_pem


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch, key_pair: tuple[str, str]) -> EntraIdentityProvider:
    _, public_pem = key_pair
    adapter = EntraIdentityProvider(
        jwks_uri="https://login.microsoftonline.com/tenant-abc/discovery/v2.0/keys",
        tenant_id=TENANT,
        audience=AUDIENCE,
    )

    class StubKey:
        key = public_pem

    monkeypatch.setattr(
        adapter._jwks_client,
        "get_signing_key_from_jwt",
        lambda _token: StubKey(),
    )
    return adapter


def sign(private_pem: str, **overrides: Any) -> str:
    now = datetime.now(tz=UTC)
    claims: dict[str, Any] = {
        "oid": "user-1",
        "tid": TENANT,
        "name": "Ada",
        "roles": ["reader"],
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": now,
        "exp": now + timedelta(hours=1),
    }
    claims.update(overrides)
    return jwt.encode(claims, private_pem, algorithm="RS256")


class TestVerification:
    async def test_a_valid_token_maps_to_a_principal(
        self, provider: EntraIdentityProvider, key_pair: tuple[str, str]
    ) -> None:
        principal = await provider.authenticate(sign(key_pair[0]))
        assert principal.subject == "user-1"
        assert principal.tenant_id == TENANT
        assert principal.roles == frozenset({"reader"})

    async def test_oid_wins_over_sub(
        self, provider: EntraIdentityProvider, key_pair: tuple[str, str]
    ) -> None:
        """'sub' is pairwise per application; 'oid' is the stable tenant identifier."""
        principal = await provider.authenticate(sign(key_pair[0], sub="pairwise-value"))
        assert principal.subject == "user-1"


class TestRejection:
    async def test_another_issuer_is_rejected(
        self, provider: EntraIdentityProvider, key_pair: tuple[str, str]
    ) -> None:
        token = sign(key_pair[0], iss="https://login.microsoftonline.com/other/v2.0")
        with pytest.raises(InvalidTokenError):
            await provider.authenticate(token)

    async def test_another_audience_is_rejected(
        self, provider: EntraIdentityProvider, key_pair: tuple[str, str]
    ) -> None:
        with pytest.raises(InvalidTokenError):
            await provider.authenticate(sign(key_pair[0], aud="api://some-other-service"))

    async def test_an_expired_token_is_rejected(
        self, provider: EntraIdentityProvider, key_pair: tuple[str, str]
    ) -> None:
        expired = datetime.now(tz=UTC) - timedelta(hours=1)
        with pytest.raises(InvalidTokenError):
            await provider.authenticate(sign(key_pair[0], exp=expired))

    async def test_a_token_missing_required_claims_is_rejected(
        self, provider: EntraIdentityProvider, key_pair: tuple[str, str]
    ) -> None:
        token = jwt.encode({"oid": "user-1", "tid": TENANT}, key_pair[0], algorithm="RS256")
        with pytest.raises(InvalidTokenError):
            await provider.authenticate(token)


class TestProviderAvailability:
    async def test_unreachable_keys_are_not_reported_as_a_bad_token(
        self, monkeypatch: pytest.MonkeyPatch, key_pair: tuple[str, str]
    ) -> None:
        """Failing to reach the key set means we cannot tell whether the token is
        valid. That is a 503, not a 401: a 401 sends every client to
        re-authenticate against a provider that is already struggling."""
        adapter = EntraIdentityProvider(
            jwks_uri="https://login.microsoftonline.com/tenant-abc/discovery/v2.0/keys",
            tenant_id=TENANT,
            audience=AUDIENCE,
        )

        def unreachable(_token: str) -> None:
            raise jwt.PyJWKClientError("could not fetch the key set")

        monkeypatch.setattr(
            adapter._jwks_client,
            "get_signing_key_from_jwt",
            unreachable,
        )

        with pytest.raises(IdentityProviderUnavailableError):
            await adapter.authenticate(sign(key_pair[0]))
