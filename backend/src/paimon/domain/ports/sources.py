"""Port for reading documents out of a system this platform does not own.

Deliberately not a port for "calling tools on an external server". A source
answers two questions — what is there, and what does one of them contain — and
that is the whole of what ingestion needs from the outside world. Whether the
answers arrive over the Model Context Protocol, an HTTP API or a mounted
directory is an infrastructure concern, and the domain is better off not knowing.

The narrowness is the security property, not an aesthetic one. Anthropic's own
guidance on consuming MCP servers is that loading an external server's tool
definitions into an agent's context costs tokens and invites tool confusion; the
OWASP description of tool poisoning is more pointed still, because a server's
tool descriptions are what a model reads and a server can change them. A port
shaped like this one cannot carry an external description into a prompt, because
there is nowhere in it for a description to go.
"""

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class SourceReference:
    """One document a source is offering, before its content is fetched.

    Listing and fetching are separate so that a run can be planned, counted and
    filtered before anything expensive happens. A repository with four thousand
    files should be refused, not discovered halfway through.

    Attributes:
        document_id: Stable identifier within the tenant. Re-using it replaces
            the previous version rather than adding a second copy.
        source_uri: Where the document came from, as a caller could follow it.
        media_type: Media type of the content, as the source describes it.
        metadata: Provenance to carry alongside the document.
    """

    document_id: str
    source_uri: str
    media_type: str
    metadata: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SourceContent:
    """A document's bytes, as fetched.

    Bytes rather than text: deciding what a byte sequence says is the parser's
    job, and a source that guessed an encoding would be making a decision it has
    no information for.

    The content is **untrusted input**. It is stored as a document's text and
    nothing else — it never becomes a tool description, a system prompt or an
    instruction — and the reason that guarantee holds is that this type is the
    only way content enters the platform from a source.
    """

    reference: SourceReference
    raw: bytes


@runtime_checkable
class DocumentSource(Protocol):
    """A system that holds documents this platform wants indexed."""

    @property
    def name(self) -> str:
        """Identifies this source in configuration, logs and errors."""
        ...

    def list(self) -> AsyncIterator[SourceReference]:
        """Yield every document this source offers, in no guaranteed order.

        An iterator rather than a list: a source may page, and a caller that has
        seen enough should be able to stop without the rest being fetched.

        Yields:
            One reference per available document.

        Raises:
            SourceUnavailableError: The source could not be reached or refused
                the credentials it was given.
        """
        ...

    async def fetch(self, reference: SourceReference) -> SourceContent:
        """Return the content behind a reference.

        Args:
            reference: One of the references this source yielded.

        Returns:
            The document's bytes.

        Raises:
            SourceUnavailableError: The source could not be reached.
            SourceContentError: The reference no longer resolves, or what came
                back was not a document.
        """
        ...
