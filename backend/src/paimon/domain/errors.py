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


class SourceError(DomainError):
    """Base class for failures of an external document source."""


class SourceUnavailableError(SourceError):
    """The source could not be reached, or refused the credentials given.

    Separate from :class:`SourceContentError` for the same reason
    :class:`IdentityProviderUnavailableError` is separate from
    :class:`InvalidTokenError`: one means a synchronisation should be retried,
    the other means retrying it will fail the same way.
    """


class SourceContentError(SourceError):
    """What the source returned was not the document that was asked for."""


class UnknownSourceError(SourceError):
    """A caller named a source this deployment does not offer.

    A caller mistake rather than a platform failure, and kept distinct so it can
    be answered as one: naming a source that was never configured is not the
    same as a configured source being unreachable.
    """


class UntrustedSourceError(SourceError):
    """A source did not present the interface it was registered with.

    Raised when an external server's tool definitions no longer match what was
    recorded for it. A server that changes a tool's description or its schema
    between sessions may have been updated, or may have been compromised; from
    here the two look identical, so the synchronisation stops and says so.
    """


class IngestionError(DomainError):
    """A document could not be ingested."""


class UnsupportedMediaTypeError(IngestionError):
    """No parser handles this media type."""


class ParseError(IngestionError):
    """The source could not be read."""
