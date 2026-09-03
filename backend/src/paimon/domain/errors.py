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


class AgentError(DomainError):
    """Base class for failures of an agent workflow."""


class AgentRunError(AgentError):
    """A run could not be completed."""


class UnknownThreadError(AgentError):
    """No run exists under that thread.

    Distinct from :class:`AgentRunError`: one means a run went wrong, the other
    means there is nothing to go wrong with. Resuming a thread that was never
    started is a caller mistake, not a platform failure.
    """


class CheckpointError(AgentError):
    """A run could not be persisted or read back."""


class AgentMemoryError(AgentError):
    """Cross-run memory could not be written or searched.

    Named for the domain rather than as ``MemoryError``, which is a Python
    builtin raised when the interpreter runs out of memory. Shadowing it would
    make an ordinary storage failure indistinguishable from exhaustion.
    """


class IngestionError(DomainError):
    """A document could not be ingested."""


class UnsupportedMediaTypeError(IngestionError):
    """No parser handles this media type."""


class ParseError(IngestionError):
    """The source could not be read."""
