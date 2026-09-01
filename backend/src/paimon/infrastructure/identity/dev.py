"""Locally signed tokens, for development and tests only.

This adapter exists so that development and the test suite never depend on
tenant connectivity. It is refused outside local and test environments by both
the settings validator and :func:`paimon.infrastructure.identity.factory.build_identity_provider`.
"""

from datetime import UTC, datetime, timedelta
from typing import Any, Final

import jwt

from paimon.domain.entities import Principal
from paimon.domain.errors import InvalidTokenError
from paimon.infrastructure.identity.claims import principal_from_claims

ALGORITHM: Final = "HS256"
DEFAULT_ISSUER: Final = "paimon-dev"
DEFAULT_AUDIENCE: Final = "paimon-local"


class DevIdentityProvider:
    """Signs and verifies tokens with a shared secret."""

    def __init__(
        self,
        signing_key: str,
        issuer: str = DEFAULT_ISSUER,
        audience: str = DEFAULT_AUDIENCE,
        leeway_seconds: int = 30,
    ) -> None:
        """Initialise the adapter.

        Args:
            signing_key: Symmetric key used to sign and verify.
            issuer: Value written to and required in the ``iss`` claim.
            audience: Value written to and required in the ``aud`` claim.
            leeway_seconds: Clock-skew tolerance applied to time-based claims.
        """
        self._signing_key = signing_key
        self._issuer = issuer
        self._audience = audience
        self._leeway = leeway_seconds

    def issue(
        self,
        subject: str,
        tenant_id: str,
        display_name: str | None = None,
        roles: frozenset[str] | None = None,
        expires_in: timedelta = timedelta(hours=1),
    ) -> str:
        """Mint a token for local use.

        Args:
            subject: Caller identifier.
            tenant_id: Organization the caller acts within.
            display_name: Optional human-readable name.
            roles: Optional role names.
            expires_in: Lifetime from now.

        Returns:
            The encoded token.
        """
        now = datetime.now(tz=UTC)
        claims: dict[str, Any] = {
            "oid": subject,
            "tid": tenant_id,
            "name": display_name,
            "roles": sorted(roles or frozenset()),
            "iss": self._issuer,
            "aud": self._audience,
            "iat": now,
            "exp": now + expires_in,
        }
        return jwt.encode(claims, self._signing_key, algorithm=ALGORITHM)

    async def authenticate(self, token: str) -> Principal:
        """Verify a locally signed token.

        Args:
            token: The raw bearer token.

        Returns:
            The authenticated caller.

        Raises:
            InvalidTokenError: The token failed verification.
        """
        try:
            claims = jwt.decode(
                token,
                self._signing_key,
                algorithms=[ALGORITHM],
                audience=self._audience,
                issuer=self._issuer,
                leeway=self._leeway,
                options={"require": ["exp", "iat", "iss", "aud"]},
            )
        except jwt.PyJWTError as error:
            msg = f"token rejected: {error}"
            raise InvalidTokenError(msg) from error
        return principal_from_claims(claims)
