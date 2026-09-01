"""Domain errors.

These are raised by the domain and by adapters implementing its ports, and are
translated into transport-specific responses at the interface boundary. Nothing
in this module knows what an HTTP status code is.
"""


class DomainError(Exception):
    """Base class for every error the domain defines."""


class AuthenticationError(DomainError):
    """The caller could not be authenticated."""


class InvalidTokenError(AuthenticationError):
    """The presented token is missing, malformed, expired or not for this audience."""


class IdentityProviderUnavailableError(AuthenticationError):
    """The identity provider could not be reached to verify the token.

    Distinct from :class:`InvalidTokenError` on purpose: one means the caller is
    not who they claim to be, the other means we cannot tell. They deserve
    different status codes, different alerts and different retry behaviour.
    """


class HealthCheckFailedError(DomainError):
    """A component reported itself unhealthy."""
