"""Helpers shared by more than one agent."""

from collections.abc import Sequence

from paimon.domain.entities import Chunk, Document
from paimon.domain.ports import DocumentRepository


async def load_documents(
    repository: DocumentRepository, sources: Sequence[Chunk], tenant_id: str
) -> dict[str, Document]:
    """Load the document behind each cited chunk, once each.

    Sources a run synthesised rather than retrieved — an incident's own timeline,
    for instance — have no document in the repository. They are simply absent
    from the result, and the citation resolver renders them from the chunk alone.
    """
    loaded: dict[str, Document] = {}
    for document_id in sorted({chunk.document_id for chunk in sources}):
        document = await repository.get(tenant_id, document_id)
        if document is not None:
            loaded[document_id] = document
    return loaded
