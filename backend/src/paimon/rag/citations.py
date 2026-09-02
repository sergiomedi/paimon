"""Turning an answer's markers into resolvable citations."""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from paimon.domain.entities import Chunk, Document
from paimon.domain.value_objects import Citation

_MARKER = re.compile(r"\[(\d+)\]")


@dataclass(frozen=True, slots=True)
class CitedAnswer:
    """An answer with its markers resolved.

    Attributes:
        text: The answer, with unresolvable markers removed.
        citations: One per distinct source the answer actually referred to, in
            marker order.
        dropped_markers: Markers the answer used that referred to no source.
    """

    text: str
    citations: tuple[Citation, ...]
    dropped_markers: tuple[int, ...]

    @property
    def is_grounded(self) -> bool:
        """Whether the answer pointed at anything a reader can check."""
        return bool(self.citations)


def resolve_citations(
    answer: str,
    sources: Sequence[Chunk],
    documents: Mapping[str, Document] | None = None,
) -> CitedAnswer:
    """Resolve an answer's ``[n]`` markers against the sources it was given.

    A marker pointing outside the source list is removed from the text. That is a
    deliberate edit of the model's output: such a marker is not content, it is a
    broken reference, and leaving it in tells the reader a claim is supported when
    nothing supports it. The count is reported rather than swallowed, because a
    model that invents references is a fact worth monitoring.

    Args:
        answer: The generated answer.
        sources: The chunks the prompt numbered, in the same order.
        documents: The documents those chunks came from, keyed by document id, so
            a citation can carry a title and a link rather than an opaque
            identifier. A missing entry degrades to the identifier.

    Returns:
        The answer with resolvable markers only, and the citations they resolve to.
    """
    used: list[int] = []
    dropped: list[int] = []

    for match in _MARKER.finditer(answer):
        marker = int(match.group(1))
        if 1 <= marker <= len(sources):
            if marker not in used:
                used.append(marker)
        elif marker not in dropped:
            dropped.append(marker)

    def strip_unresolvable(match: re.Match[str]) -> str:
        marker = int(match.group(1))
        return match.group(0) if 1 <= marker <= len(sources) else ""

    cleaned = _MARKER.sub(strip_unresolvable, answer)
    # Removing a marker can leave a doubled space or a space before punctuation.
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\s+([.,;:!?])", r"\1", cleaned).strip()

    documents = documents or {}
    citations = tuple(
        _citation(marker, sources[marker - 1], documents.get(sources[marker - 1].document_id))
        for marker in used
    )
    return CitedAnswer(text=cleaned, citations=citations, dropped_markers=tuple(dropped))


def _citation(marker: int, chunk: Chunk, document: Document | None) -> Citation:
    """Build a citation, filling in what the document knows and the chunk does not."""
    source_uri = document.source_uri if document else chunk.document_id
    title = document.title if document else chunk.document_id
    return Citation(
        marker=marker,
        document_id=chunk.document_id,
        chunk_id=chunk.chunk_id,
        source_uri=source_uri,
        title=title,
        heading_path=chunk.heading_path,
        start_char=chunk.start_char,
        end_char=chunk.end_char,
        quote=chunk.text,
    )
