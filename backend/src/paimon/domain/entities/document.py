"""Documents and the chunks they are split into."""

from collections.abc import Mapping
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Document:
    """A source document after parsing and normalization.

    ``text`` is the normalized form, and it is the coordinate system every chunk's
    character offsets refer to. Keeping one canonical text is what allows a
    citation to be resolved back to an exact span later.

    Attributes:
        document_id: Stable identifier within the tenant.
        tenant_id: Owning organization.
        source_uri: Where the document came from, for provenance and re-ingestion.
        title: Human-readable title.
        text: Normalized document text.
        content_hash: Hash of the normalized text, used to skip unchanged work.
        media_type: Media type of the original source.
        metadata: Free-form provenance, kept as strings so it survives any store.
    """

    document_id: str
    tenant_id: str
    source_uri: str
    title: str
    text: str
    content_hash: str
    media_type: str
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Reject a document that cannot be identified or cited.

        Raises:
            ValueError: If an identifying field is blank or the text is empty.
        """
        for name in ("document_id", "tenant_id", "content_hash"):
            if not str(getattr(self, name)).strip():
                msg = f"a document requires a non-empty {name}"
                raise ValueError(msg)
        if not self.text:
            msg = "a document with no text cannot be chunked or cited"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class Chunk:
    """A retrievable span of a document.

    The character offsets are the point of this type. Without them a citation can
    say "this came from runbook.md" and no more; with them it resolves to the
    exact span the claim rests on, which is the difference between a citation and
    a gesture towards one.

    Attributes:
        chunk_id: Stable identifier, derived from the document and ordinal.
        document_id: The document this span belongs to.
        tenant_id: Owning organization, carried so the store can isolate on it.
        ordinal: Position within the document, starting at zero.
        text: The chunk's text, which may be prefixed with its heading path.
        heading_path: Enclosing headings, outermost first.
        start_char: Inclusive start offset into the document's normalized text.
        end_char: Exclusive end offset into the document's normalized text.
        token_count: Tokens in the chunk, for budgeting a prompt.
    """

    chunk_id: str
    document_id: str
    tenant_id: str
    ordinal: int
    text: str
    start_char: int
    end_char: int
    token_count: int
    heading_path: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Reject a chunk that cannot be located in its document.

        Raises:
            ValueError: If identifiers are blank, the text is empty, the ordinal is
                negative, or the offsets do not describe a forward span.
        """
        for name in ("chunk_id", "document_id", "tenant_id"):
            if not str(getattr(self, name)).strip():
                msg = f"a chunk requires a non-empty {name}"
                raise ValueError(msg)
        if not self.text.strip():
            msg = "a chunk with no text is not retrievable"
            raise ValueError(msg)
        if self.ordinal < 0:
            msg = "a chunk ordinal cannot be negative"
            raise ValueError(msg)
        if self.start_char < 0 or self.end_char <= self.start_char:
            msg = f"invalid span [{self.start_char}, {self.end_char})"
            raise ValueError(msg)
        if self.token_count <= 0:
            msg = "a chunk must contain at least one token"
            raise ValueError(msg)

    @property
    def heading_trail(self) -> str:
        """The heading path rendered for display and for prompt context."""
        return " > ".join(self.heading_path)

    @property
    def embedding_text(self) -> str:
        """The text to embed, which is not the text to cite.

        A chunk taken from deep inside a document often reads as a fragment: "Run
        this before the upgrade" says nothing about what "this" is. Prefixing the
        enclosing headings restores the context the author left implicit, and
        measurably improves retrieval.

        The cited text stays exactly ``text`` — a verbatim slice of the document —
        so that the character offsets remain truthful. Embedding one string and
        citing another is deliberate: the index is for finding, the offsets are
        for proving.
        """
        if not self.heading_path:
            return self.text
        return f"{self.heading_trail}\n\n{self.text}"
