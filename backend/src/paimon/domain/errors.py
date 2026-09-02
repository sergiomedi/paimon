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


class RetrievalError(DomainError):
    """Retrieval could not be completed."""


class IndexMismatchError(RetrievalError):
    """An embedding does not match the index it was offered to.

    Raised when the embedding model or dimensionality differs from what the index
    declares. Mixing models in one index degrades retrieval without ever raising,
    so the mismatch is refused at the boundary.
    """


class EmbeddingError(DomainError):
    """The provider could not produce an embedding."""


class GenerationError(DomainError):
    """The provider could not produce an answer."""


class IngestionError(DomainError):
    """A document could not be ingested."""


class UnsupportedMediaTypeError(IngestionError):
    """No parser handles this media type."""


class ParseError(IngestionError):
    """The source could not be read."""
