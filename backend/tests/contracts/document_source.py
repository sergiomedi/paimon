"""Contract for the DocumentSource port.

What every source must do, whatever it reads from. The assertions are about
shape and behaviour — listing is repeatable, a reference round-trips to its
content, an id identifies one document — and say nothing about what a particular
source holds, because a contract that knew that could only ever be satisfied by
one adapter.

The suite is what keeps the port honest. A port with a single implementation is
indistinguishable from that implementation's interface, and the way to tell the
difference is to make something else pass the same assertions.
"""

import pytest

from paimon.domain.errors import SourceContentError
from paimon.domain.ports import DocumentSource, SourceReference

#: What a subclass must make its source offer. Two documents, because most of
#: the mistakes worth catching only appear once there is more than one.
REQUIRED = {
    "alpha": b"# Alpha\n\nThe first document.\n",
    "beta": b"# Beta\n\nThe second document.\n",
}


class DocumentSourceContract:
    """Every DocumentSource adapter must pass these."""

    @pytest.fixture
    def source(self) -> DocumentSource:
        """Supplied by the subclass, offering exactly :data:`REQUIRED`."""
        raise NotImplementedError

    async def _references(self, source: DocumentSource) -> list[SourceReference]:
        return [reference async for reference in source.list()]

    async def test_it_lists_what_it_holds(self, source: DocumentSource) -> None:
        references = await self._references(source)
        assert len(references) == len(REQUIRED)

    async def test_every_reference_is_complete(self, source: DocumentSource) -> None:
        # A reference with no uri cannot be cited, and one with no media type
        # cannot be parsed. Both failures surface far from here.
        for reference in await self._references(source):
            assert reference.document_id
            assert reference.source_uri
            assert reference.media_type

    async def test_document_ids_are_unique(self, source: DocumentSource) -> None:
        # Ingestion is keyed by document id: two documents sharing one would
        # silently overwrite each other, and the corpus would be short by one
        # with nothing anywhere reporting a problem.
        ids = [reference.document_id for reference in await self._references(source)]
        assert len(set(ids)) == len(ids)

    async def test_a_reference_fetches_the_content_it_points_at(
        self, source: DocumentSource
    ) -> None:
        for reference in await self._references(source):
            content = await source.fetch(reference)
            assert content.reference == reference
            assert content.raw

    async def test_listing_twice_gives_the_same_documents(self, source: DocumentSource) -> None:
        # A synchronisation lists and then fetches. A source whose listing is not
        # repeatable turns that into a race with itself.
        first = {reference.document_id for reference in await self._references(source)}
        second = {reference.document_id for reference in await self._references(source)}
        assert first == second

    async def test_content_is_bytes_and_is_not_decoded(self, source: DocumentSource) -> None:
        # The port carries bytes on purpose: guessing an encoding is the parser's
        # decision, made with information a source does not have.
        reference = (await self._references(source))[0]
        assert isinstance((await source.fetch(reference)).raw, bytes)

    async def test_a_reference_that_no_longer_resolves_is_refused(
        self, source: DocumentSource
    ) -> None:
        stale = SourceReference(
            document_id="gone",
            source_uri="nowhere://gone",
            media_type="text/markdown",
            metadata={"path": "gone.md"},
        )
        with pytest.raises(SourceContentError):
            await source.fetch(stale)

    async def test_the_source_names_itself(self, source: DocumentSource) -> None:
        assert source.name
