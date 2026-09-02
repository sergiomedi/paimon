"""Citations as a domain value object."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Citation:
    """A claim's anchor in a source document.

    Carries both the quoted span and the offsets it came from. The quote is what a
    reader sees; the offsets are what lets the platform open the source document
    and show the passage in the context it was written in — which is the whole
    difference between a citation and a filename.

    Attributes:
        marker: The number the answer used to refer to this source, from one.
        document_id: The cited document.
        chunk_id: The cited chunk.
        source_uri: Where the document came from.
        title: The document's title, for display.
        heading_path: Enclosing headings of the cited span.
        start_char: Inclusive offset into the document's normalized text.
        end_char: Exclusive offset into the document's normalized text.
        quote: The cited text, verbatim.
    """

    marker: int
    document_id: str
    chunk_id: str
    source_uri: str
    title: str
    heading_path: tuple[str, ...]
    start_char: int
    end_char: int
    quote: str

    def __post_init__(self) -> None:
        """Reject a citation that cannot be resolved.

        Raises:
            ValueError: If the marker is not a positive position, or the span does
                not move forward.
        """
        if self.marker < 1:
            msg = "citation markers are one-based"
            raise ValueError(msg)
        if self.start_char < 0 or self.end_char <= self.start_char:
            msg = f"invalid citation span [{self.start_char}, {self.end_char})"
            raise ValueError(msg)
