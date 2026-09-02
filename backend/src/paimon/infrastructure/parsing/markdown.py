"""Markdown and plain-text parsing."""

import re

from paimon.domain.errors import ParseError, UnsupportedMediaTypeError
from paimon.domain.ports import ParsedDocument

_TITLE = re.compile(r"^#\s+(?P<title>.+?)\s*#*\s*$", re.MULTILINE)
_EXCESS_BLANK_LINES = re.compile(r"\n{3,}")
_TRAILING_SPACE = re.compile(r"[ \t]+$", re.MULTILINE)


class MarkdownParser:
    """Normalizes Markdown and plain text into the document's canonical form.

    Normalization happens once, here, and everything downstream works from the
    result: chunk offsets index into it and citations resolve through it. If two
    ingestions of the same bytes could normalize differently, identical documents
    would produce different content hashes and re-index forever, so every step is
    deterministic.

    The parser does not chunk and does not interpret structure beyond preserving
    it. That keeps the chunking strategy free to change without touching a single
    parser.
    """

    MEDIA_TYPES = frozenset(
        {"text/markdown", "text/x-markdown", "text/plain", "application/markdown"}
    )

    @property
    def supported_media_types(self) -> frozenset[str]:
        """Media types this parser accepts."""
        return self.MEDIA_TYPES

    async def parse(self, raw: bytes, media_type: str) -> ParsedDocument:
        """Decode and normalize a source document.

        Args:
            raw: The source bytes.
            media_type: Media type of the source; parameters such as ``charset``
                are ignored.

        Returns:
            The normalized document and its title, when it has one.

        Raises:
            UnsupportedMediaTypeError: If this parser does not handle the type.
            ParseError: If the bytes are not valid UTF-8, or decode to nothing.
        """
        base_type = media_type.split(";", maxsplit=1)[0].strip().lower()
        if base_type not in self.MEDIA_TYPES:
            msg = f"MarkdownParser does not handle '{media_type}'"
            raise UnsupportedMediaTypeError(msg)

        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            msg = f"source is not valid UTF-8: {error}"
            raise ParseError(msg) from error

        text = self._normalize(text)
        if not text:
            msg = "source decoded to no content"
            raise ParseError(msg)

        title_match = _TITLE.search(text)
        return ParsedDocument(
            text=text,
            title=title_match.group("title") if title_match else None,
            metadata={"media_type": base_type},
        )

    @staticmethod
    def _normalize(text: str) -> str:
        """Reduce the text to one canonical form."""
        text = text.lstrip("﻿")
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = _TRAILING_SPACE.sub("", text)
        text = _EXCESS_BLANK_LINES.sub("\n\n", text)
        return text.strip()
