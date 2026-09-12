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

API_URI_PREFIX: Final = "api://"


def accepted_audiences(audience: str) -> list[str]:
    """Both spellings Microsoft Entra uses for one API's identifier.

    An access token's ``aud`` claim names the API it was minted for, and Entra
    spells that name two ways depending on a property of the *app registration*
    rather than on anything the caller does: a v1.0 token carries the application
    ID URI (``api://<app id>``) and a v2.0 token carries the bare application id.
    ``requestedAccessTokenVersion`` decides which, and the same client asking the
    same way gets a different ``aud`` on either side of that setting.

    So one configured value is read as the identifier it is, and the other
    spelling of the same identifier is derived. This is not a widening of who is
    accepted: both strings name this one API in this one tenant, and a token for
    anything else still matches neither.

    It is here because the alternative is worse in a specific way. The audience
    is baked into the container at deployment time, so choosing the wrong
    spelling is not a setting to correct — it is an API that rejects every token
    until the next deployment, discovered at the first call.

    Args:
        audience: The configured audience, in either spelling.

    Returns:
        Both spellings, the configured one first.
    """
    configured = audience.strip()
    if configured.startswith(API_URI_PREFIX):
        return [configured, configured.removeprefix(API_URI_PREFIX)]
    return [configured, f"{API_URI_PREFIX}{configured}"]


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
            audience: Expected ``aud`` claim, in either of the two spellings
                Entra issues — see :func:`accepted_audiences`, which derives the
                other one.
            jwks_cache_seconds: How long signing keys are cached.
            leeway_seconds: Clock-skew tolerance applied to time-based claims.
        """
        self._audiences = accepted_audiences(audience)
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
                audience=self._audiences,
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
