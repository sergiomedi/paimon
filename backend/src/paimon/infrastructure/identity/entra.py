"""Microsoft Entra ID token validation (ADR-0004).

Validation is stateless: the token's signature is checked against the tenant's
published keys, and the issuer, audience and time claims are enforced. The
platform never sees a credential and keeps no session.
"""

import asyncio
from typing import Final

import jwt
from jwt import PyJWKClient

from paimon.domain.entities import Principal
from paimon.domain.errors import IdentityProviderUnavailableError, InvalidTokenError
from paimon.infrastructure.identity.claims import principal_from_claims

ALGORITHMS: Final = ["RS256"]


class EntraIdentityProvider:
    """Verifies tokens issued by an Entra ID tenant."""

    def __init__(
        self,
        jwks_uri: str,
        tenant_id: str,
        audience: str,
        jwks_cache_seconds: int = 3600,
        leeway_seconds: int = 30,
    ) -> None:
        """Initialise the adapter.

        Args:
            jwks_uri: The tenant's JSON Web Key Set endpoint.
            tenant_id: Tenant the token must have been issued by.
            audience: Expected ``aud`` claim, normally the application's client id.
            jwks_cache_seconds: How long signing keys are cached.
            leeway_seconds: Clock-skew tolerance applied to time-based claims.
        """
        self._audience = audience
        self._issuer = f"https://login.microsoftonline.com/{tenant_id}/v2.0"
        self._leeway = leeway_seconds
        # PyJWKClient caches keys and refetches on an unknown key id, which is
        # what makes routine key rotation invisible to us.
        self._jwks_client = PyJWKClient(
            jwks_uri,
            cache_keys=True,
            lifespan=jwks_cache_seconds,
        )

    async def authenticate(self, token: str) -> Principal:
        """Verify a token issued by the tenant.

        Args:
            token: The raw bearer token.

        Returns:
            The authenticated caller.

        Raises:
            InvalidTokenError: The token failed verification.
            IdentityProviderUnavailableError: The signing keys could not be fetched.
        """
        signing_key = await self._signing_key_for(token)
        try:
            claims = jwt.decode(
                token,
                signing_key,
                algorithms=ALGORITHMS,
                audience=self._audience,
                issuer=self._issuer,
                leeway=self._leeway,
                options={"require": ["exp", "iat", "iss", "aud"]},
            )
        except jwt.PyJWTError as error:
            msg = f"token rejected: {error}"
            raise InvalidTokenError(msg) from error
        return principal_from_claims(claims)

    async def _signing_key_for(self, token: str) -> str:
        """Resolve the signing key for a token's key id.

        PyJWKClient performs blocking HTTP, so it runs in a worker thread rather
        than stalling the event loop for every request that arrives after a key
        rotation.
        """
        try:
            jwk = await asyncio.to_thread(self._jwks_client.get_signing_key_from_jwt, token)
        except jwt.PyJWKClientError as error:
            # Failing to reach the key set is not the caller's fault: it means we
            # cannot tell whether the token is valid, which is a 503, not a 401.
            msg = f"could not retrieve signing keys: {error}"
            raise IdentityProviderUnavailableError(msg) from error
        except jwt.PyJWTError as error:
            msg = f"token rejected: {error}"
            raise InvalidTokenError(msg) from error
        return str(jwk.key)
