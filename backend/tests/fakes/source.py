"""A document source held in memory."""

from collections.abc import AsyncIterator, Mapping

from paimon.domain.errors import SourceContentError, SourceUnavailableError
from paimon.domain.ports import SourceContent, SourceReference


class InMemoryDocumentSource:
    """Offers a fixed set of documents, and can be made to fail on demand.

    The failure modes are part of the fixture rather than something to patch in,
    because they are what the port's callers are supposed to handle: a source
    that cannot be reached at all, and a single document that cannot be read.
    """

    def __init__(
        self,
        documents: Mapping[str, bytes],
        *,
        name: str = "in-memory",
        unreadable: frozenset[str] = frozenset(),
        unreachable: bool = False,
    ) -> None:
        """Initialise the source.

        Args:
            documents: Document id to content.
            name: How this source names itself.
            unreadable: Document ids that fail when fetched.
            unreachable: Whether listing fails outright.
        """
        self._documents = dict(documents)
        self._name = name
        self._unreadable = unreadable
        self._unreachable = unreachable
        self.fetched: list[str] = []

    @property
    def name(self) -> str:
        """Identifies this source."""
        return self._name

    async def list(self) -> AsyncIterator[SourceReference]:
        """Yield a reference per document."""
        if self._unreachable:
            msg = f"source '{self._name}' is unreachable"
            raise SourceUnavailableError(msg)
        for document_id in self._documents:
            yield SourceReference(
                document_id=document_id,
                source_uri=f"memory://{self._name}/{document_id}",
                media_type="text/markdown",
                metadata={"source": self._name},
            )

    async def fetch(self, reference: SourceReference) -> SourceContent:
        """Return a document's bytes."""
        if self._unreachable:
            msg = f"source '{self._name}' is unreachable"
            raise SourceUnavailableError(msg)
        if reference.document_id in self._unreadable:
            msg = f"'{reference.document_id}' could not be read"
            raise SourceContentError(msg)
        raw = self._documents.get(reference.document_id)
        if raw is None:
            msg = f"'{reference.document_id}' no longer exists"
            raise SourceContentError(msg)
        self.fetched.append(reference.document_id)
        return SourceContent(reference=reference, raw=raw)
