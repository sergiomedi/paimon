"""Port for verifying a caller's identity."""

from typing import Protocol, runtime_checkable

from paimon.domain.entities import Principal


@runtime_checkable
class IdentityProvider(Protocol):
    """Verifies a bearer token and maps it to a :class:`Principal`.

    The platform validates tokens; it never issues or stores credentials
    (ADR-0004). Implementations must not perform authorization — establishing
    *who* the caller is and deciding *what* they may do are separate concerns
    with separate reasons to change.
    """

    async def authenticate(self, token: str) -> Principal:
        """Verify a token and return the caller it identifies.

        Args:
            token: The raw bearer token, without the ``Bearer`` prefix.

        Returns:
            The authenticated caller.

        Raises:
            InvalidTokenError: The token is malformed, expired, wrongly signed
                or not intended for this audience.
            IdentityProviderUnavailableError: Verification could not be completed
                because the provider was unreachable.
        """
        ...
