"""Tests for the Markdown parser."""

import pytest

from paimon.domain.errors import ParseError, UnsupportedMediaTypeError
from paimon.infrastructure.parsing import MarkdownParser


@pytest.fixture
def parser() -> MarkdownParser:
    return MarkdownParser()


class TestNormalization:
    async def test_windows_line_endings_become_unix(self, parser: MarkdownParser) -> None:
        """The same bytes must normalize identically wherever they were authored,
        or one document acquires two content hashes and re-indexes forever."""
        parsed = await parser.parse(b"# Title\r\n\r\nBody.\r\n", "text/markdown")
        assert parsed.text == "# Title\n\nBody."

    async def test_a_byte_order_mark_is_stripped(self, parser: MarkdownParser) -> None:
        parsed = await parser.parse("﻿# Title\n\nBody.".encode(), "text/markdown")
        assert parsed.text.startswith("# Title")

    async def test_runs_of_blank_lines_collapse(self, parser: MarkdownParser) -> None:
        parsed = await parser.parse(b"# Title\n\n\n\n\nBody.", "text/markdown")
        assert parsed.text == "# Title\n\nBody."

    async def test_trailing_whitespace_is_removed(self, parser: MarkdownParser) -> None:
        parsed = await parser.parse(b"# Title   \n\nBody.\t\n", "text/markdown")
        assert parsed.text == "# Title\n\nBody."

    async def test_normalization_is_idempotent(self, parser: MarkdownParser) -> None:
        once = await parser.parse(b"# Title\r\n\r\n\r\nBody.  \r\n", "text/markdown")
        twice = await parser.parse(once.text.encode(), "text/markdown")
        assert once.text == twice.text


class TestTitle:
    async def test_the_first_level_one_heading_becomes_the_title(
        self, parser: MarkdownParser
    ) -> None:
        parsed = await parser.parse(b"# Node maintenance\n\n## Draining\n", "text/markdown")
        assert parsed.title == "Node maintenance"

    async def test_a_document_without_a_heading_has_no_title(self, parser: MarkdownParser) -> None:
        parsed = await parser.parse(b"Just prose.", "text/plain")
        assert parsed.title is None


class TestAcceptance:
    @pytest.mark.parametrize(
        "media_type",
        ["text/markdown", "text/plain", "text/markdown; charset=utf-8", "TEXT/MARKDOWN"],
    )
    async def test_supported_types_are_accepted(
        self, parser: MarkdownParser, media_type: str
    ) -> None:
        parsed = await parser.parse(b"# Title\n\nBody.", media_type)
        assert parsed.text == "# Title\n\nBody."

    async def test_an_unsupported_type_is_refused(self, parser: MarkdownParser) -> None:
        with pytest.raises(UnsupportedMediaTypeError, match="does not handle"):
            await parser.parse(b"%PDF-1.7", "application/pdf")

    async def test_invalid_utf8_is_refused(self, parser: MarkdownParser) -> None:
        with pytest.raises(ParseError, match="not valid UTF-8"):
            await parser.parse(b"\xff\xfe\x00invalid", "text/markdown")

    async def test_content_free_input_is_refused(self, parser: MarkdownParser) -> None:
        """An empty document would produce no chunks and no citations."""
        with pytest.raises(ParseError, match="no content"):
            await parser.parse(b"   \n\n  \n", "text/markdown")

    def test_it_declares_what_it_supports(self, parser: MarkdownParser) -> None:
        assert "text/markdown" in parser.supported_media_types
