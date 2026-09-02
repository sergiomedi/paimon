"""Port for turning source bytes into normalized text."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    """A source document reduced to normalized text and what came with it.

    Parsers normalize to Markdown-like text and stop there. They do not chunk and
    do not interpret structure beyond preserving it, so that the chunking strategy
    can change without touching a single parser.
    """

    text: str
    title: str | None = None
    metadata: Mapping[str, str] = field(default_factory=dict)


@runtime_checkable
class DocumentParser(Protocol):
    """Converts a supported source format into normalized text."""

    @property
    def supported_media_types(self) -> frozenset[str]:
        """Media types this parser accepts."""
        ...

    async def parse(self, raw: bytes, media_type: str) -> ParsedDocument:
        """Parse a document.

        Args:
            raw: The source bytes.
            media_type: Media type of the source.

        Returns:
            The normalized document.

        Raises:
            UnsupportedMediaTypeError: If this parser does not handle the type.
            ParseError: If the source could not be read.
        """
        ...
