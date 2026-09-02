"""Tests for citation resolution."""

from paimon.domain.entities import Chunk, Document
from paimon.rag.citations import resolve_citations


def chunk(chunk_id: str, text: str = "Cordon the node first.") -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        document_id="runbook",
        tenant_id="tenant-1",
        ordinal=0,
        text=text,
        start_char=40,
        end_char=40 + len(text),
        token_count=5,
        heading_path=("Runbooks", "Draining"),
    )


DOCUMENT = Document(
    document_id="runbook",
    tenant_id="tenant-1",
    source_uri="https://example.test/runbook.md",
    title="Node maintenance",
    text="x" * 200,
    content_hash="hash",
    media_type="text/markdown",
)


class TestResolution:
    def test_a_marker_resolves_to_its_source(self) -> None:
        cited = resolve_citations("Cordon it first [1].", [chunk("c1")])

        assert cited.is_grounded
        assert cited.citations[0].chunk_id == "c1"
        assert cited.citations[0].marker == 1

    def test_the_citation_carries_the_span_it_came_from(self) -> None:
        """Offsets are what let a client open the document at the passage rather
        than at the top."""
        (citation,) = resolve_citations("Yes [1].", [chunk("c1")]).citations

        assert (citation.start_char, citation.end_char) == (40, 40 + len("Cordon the node first."))
        assert citation.quote == "Cordon the node first."
        assert citation.heading_path == ("Runbooks", "Draining")

    def test_document_details_enrich_the_citation(self) -> None:
        (citation,) = resolve_citations("Yes [1].", [chunk("c1")], {"runbook": DOCUMENT}).citations

        assert citation.title == "Node maintenance"
        assert citation.source_uri == "https://example.test/runbook.md"

    def test_a_missing_document_degrades_to_the_identifier(self) -> None:
        (citation,) = resolve_citations("Yes [1].", [chunk("c1")]).citations
        assert citation.title == "runbook"

    def test_a_repeated_marker_cites_once(self) -> None:
        cited = resolve_citations("First [1]. Also [1].", [chunk("c1")])
        assert len(cited.citations) == 1

    def test_citations_come_back_in_marker_order(self) -> None:
        cited = resolve_citations("Second [2]. First [1].", [chunk("c1"), chunk("c2")])
        assert [citation.marker for citation in cited.citations] == [2, 1]


class TestInventedReferences:
    def test_a_marker_outside_the_source_list_is_removed(self) -> None:
        """Leaving it in tells the reader a claim is supported when nothing
        supports it."""
        cited = resolve_citations("Grounded [1]. Invented [7].", [chunk("c1")])

        assert "[7]" not in cited.text
        assert "[1]" in cited.text

    def test_removed_markers_are_reported_rather_than_swallowed(self) -> None:
        """A model that invents references is a fact worth monitoring."""
        cited = resolve_citations("Invented [7] and [9].", [chunk("c1")])
        assert cited.dropped_markers == (7, 9)

    def test_removal_does_not_leave_ragged_punctuation(self) -> None:
        cited = resolve_citations("The node is drained [4].", [chunk("c1")])
        assert cited.text == "The node is drained."

    def test_an_answer_with_no_markers_is_not_grounded(self) -> None:
        """The whole point: an uncited answer is one the reader cannot check."""
        cited = resolve_citations("The node is drained.", [chunk("c1")])

        assert cited.citations == ()
        assert cited.is_grounded is False
