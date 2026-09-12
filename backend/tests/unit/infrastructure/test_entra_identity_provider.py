"""Tests for the Entra ID adapter.

The tenant's signing keys are replaced by a locally generated RSA pair, so the
adapter's own verification logic — algorithm, audience, issuer, time claims and
the distinction between a bad token and an unreachable provider — is exercised
without a network call or a tenant.
"""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt import PyJWK
from jwt.algorithms import RSAAlgorithm

from paimon.domain.errors import IdentityProviderUnavailableError, InvalidTokenError
from paimon.infrastructure.identity import EntraIdentityProvider
from paimon.infrastructure.identity.entra import accepted_audiences

TENANT = "tenant-abc"
AUDIENCE = "api://paimon"
ISSUER = f"https://login.microsoftonline.com/{TENANT}/v2.0"


@pytest.fixture(scope="module")
def key_pair() -> tuple[str, PyJWK]:
    """A signing key, in the two forms this test needs.

    The private half as a PEM to sign with; the public half as a ``PyJWK``,
    which is exactly what the tenant's key set endpoint yields once
    ``PyJWKClient`` has parsed it.
    """
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    published = json.loads(RSAAlgorithm.to_jwk(private_key.public_key()))
    return private_pem, PyJWK(published, algorithm="RS256")


def build(
    monkeypatch: pytest.MonkeyPatch, public_key: PyJWK, audience: str
) -> EntraIdentityProvider:
    """An adapter whose key set is the locally generated pair.

    The stand-in returns a **PyJWK**, because that is what the real
    ``PyJWKClient`` returns. It used to return an object whose ``key`` attribute
    was a PEM string, and that single liberty hid a defect from Phase 1 until the
    first request ever made to a deployed environment: the adapter passed
    ``str(jwk.key)`` to PyJWT, which on a real key is
    ``<cryptography…RSAPublicKey object at 0x7f…>``, and every token was rejected
    as unparseable. A double that answers in a shape the real thing never
    produces tests the double.
    """
    adapter = EntraIdentityProvider(
        jwks_uri="https://login.microsoftonline.com/tenant-abc/discovery/v2.0/keys",
        tenant_id=TENANT,
        audience=audience,
    )
    monkeypatch.setattr(
        adapter._jwks_client,
        "get_signing_key_from_jwt",
        lambda _token: public_key,
    )
    return adapter


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch, key_pair: tuple[str, PyJWK]) -> EntraIdentityProvider:
    return build(monkeypatch, key_pair[1], AUDIENCE)


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
        self, provider: EntraIdentityProvider, key_pair: tuple[str, PyJWK]
    ) -> None:
        principal = await provider.authenticate(sign(key_pair[0]))
        assert principal.subject == "user-1"
        assert principal.tenant_id == TENANT
        assert principal.roles == frozenset({"reader"})

    async def test_oid_wins_over_sub(
        self, provider: EntraIdentityProvider, key_pair: tuple[str, PyJWK]
    ) -> None:
        """'sub' is pairwise per application; 'oid' is the stable tenant identifier."""
        principal = await provider.authenticate(sign(key_pair[0], sub="pairwise-value"))
        assert principal.subject == "user-1"


class TestRejection:
    async def test_another_issuer_is_rejected(
        self, provider: EntraIdentityProvider, key_pair: tuple[str, PyJWK]
    ) -> None:
        token = sign(key_pair[0], iss="https://login.microsoftonline.com/other/v2.0")
        with pytest.raises(InvalidTokenError):
            await provider.authenticate(token)

    async def test_another_audience_is_rejected(
        self, provider: EntraIdentityProvider, key_pair: tuple[str, PyJWK]
    ) -> None:
        with pytest.raises(InvalidTokenError):
            await provider.authenticate(sign(key_pair[0], aud="api://some-other-service"))

    async def test_an_expired_token_is_rejected(
        self, provider: EntraIdentityProvider, key_pair: tuple[str, PyJWK]
    ) -> None:
        expired = datetime.now(tz=UTC) - timedelta(hours=1)
        with pytest.raises(InvalidTokenError):
            await provider.authenticate(sign(key_pair[0], exp=expired))

    async def test_a_token_missing_required_claims_is_rejected(
        self, provider: EntraIdentityProvider, key_pair: tuple[str, PyJWK]
    ) -> None:
        token = jwt.encode({"oid": "user-1", "tid": TENANT}, key_pair[0], algorithm="RS256")
        with pytest.raises(InvalidTokenError):
            await provider.authenticate(token)


class TestAudienceSpelling:
    """One API, two spellings, and the caller does not choose between them.

    A v1.0 access token names this API by its application ID URI and a v2.0 token
    names it by the bare application id. Which arrives depends on the app
    registration's ``requestedAccessTokenVersion``, so an API that accepts only
    the spelling somebody configured rejects every token the day that property
    changes — and the audience is fixed at deployment time, so the discovery is a
    redeployment away from the fix.
    """

    def test_the_pair_is_one_identifier_in_both_spellings(self) -> None:
        assert accepted_audiences("api://abc") == ["api://abc", "abc"]
        assert accepted_audiences("abc") == ["abc", "api://abc"]

    async def test_the_bare_application_id_is_accepted(
        self, provider: EntraIdentityProvider, key_pair: tuple[str, PyJWK]
    ) -> None:
        """Configured as api://paimon, and a v2.0 token says paimon."""
        principal = await provider.authenticate(sign(key_pair[0], aud="paimon"))
        assert principal.subject == "user-1"

    async def test_the_uri_form_is_accepted_when_the_bare_id_is_configured(
        self, monkeypatch: pytest.MonkeyPatch, key_pair: tuple[str, PyJWK]
    ) -> None:
        """And the same holds the other way round, which is the point of a pair."""
        adapter = build(monkeypatch, key_pair[1], "paimon")
        principal = await adapter.authenticate(sign(key_pair[0], aud="api://paimon"))
        assert principal.subject == "user-1"


class TestProviderAvailability:
    async def test_unreachable_keys_are_not_reported_as_a_bad_token(
        self, monkeypatch: pytest.MonkeyPatch, key_pair: tuple[str, PyJWK]
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
