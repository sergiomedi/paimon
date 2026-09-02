"""Tests for the Document and Chunk entities."""

import pytest

from paimon.domain.entities import Chunk, Document


def document(**overrides: object) -> Document:
    fields: dict[str, object] = {
        "document_id": "doc-1",
        "tenant_id": "tenant-1",
        "source_uri": "https://example.test/runbook.md",
        "title": "Draining a node",
        "text": "# Draining a node\n\nCordon first.",
        "content_hash": "abc123",
        "media_type": "text/markdown",
    }
    fields.update(overrides)
    return Document(**fields)  # type: ignore[arg-type]


def chunk(**overrides: object) -> Chunk:
    fields: dict[str, object] = {
        "chunk_id": "doc-1:0",
        "document_id": "doc-1",
        "tenant_id": "tenant-1",
        "ordinal": 0,
        "text": "Cordon first.",
        "start_char": 20,
        "end_char": 33,
        "token_count": 2,
    }
    fields.update(overrides)
    return Chunk(**fields)  # type: ignore[arg-type]


class TestDocument:
    def test_it_is_immutable(self) -> None:
        with pytest.raises(AttributeError):
            document().text = "rewritten"  # type: ignore[misc]

    @pytest.mark.parametrize("field", ["document_id", "tenant_id", "content_hash"])
    def test_an_identifying_field_cannot_be_blank(self, field: str) -> None:
        with pytest.raises(ValueError, match=f"non-empty {field}"):
            document(**{field: "  "})

    def test_a_document_without_text_is_refused(self) -> None:
        """It could be neither chunked nor cited, so it has no place in the index."""
        with pytest.raises(ValueError, match="cannot be chunked or cited"):
            document(text="")


class TestChunk:
    def test_the_heading_trail_reads_outermost_first(self) -> None:
        assert chunk(heading_path=("Runbooks", "Node maintenance")).heading_trail == (
            "Runbooks > Node maintenance"
        )

    def test_a_chunk_with_no_headings_has_an_empty_trail(self) -> None:
        assert chunk().heading_trail == ""

    @pytest.mark.parametrize("field", ["chunk_id", "document_id", "tenant_id"])
    def test_an_identifying_field_cannot_be_blank(self, field: str) -> None:
        with pytest.raises(ValueError, match=f"non-empty {field}"):
            chunk(**{field: " "})

    def test_whitespace_only_text_is_refused(self) -> None:
        with pytest.raises(ValueError, match="not retrievable"):
            chunk(text="   \n ")

    def test_a_negative_ordinal_is_refused(self) -> None:
        with pytest.raises(ValueError, match="ordinal cannot be negative"):
            chunk(ordinal=-1)

    @pytest.mark.parametrize(("start", "end"), [(-1, 10), (10, 10), (10, 5)])
    def test_a_span_that_does_not_move_forward_is_refused(self, start: int, end: int) -> None:
        """The offsets are what a citation resolves through; a broken span is a
        citation that points nowhere."""
        with pytest.raises(ValueError, match="invalid span"):
            chunk(start_char=start, end_char=end)

    def test_a_chunk_with_no_tokens_is_refused(self) -> None:
        with pytest.raises(ValueError, match="at least one token"):
            chunk(token_count=0)
