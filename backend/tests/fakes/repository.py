"""An in-memory DocumentRepository."""

from paimon.domain.entities import Document


class InMemoryDocumentRepository:
    """Reference implementation of the DocumentRepository port."""

    def __init__(self) -> None:
        self._documents: dict[tuple[str, str], Document] = {}

    async def get(self, tenant_id: str, document_id: str) -> Document | None:
        return self._documents.get((tenant_id, document_id))

    async def save(self, document: Document) -> None:
        self._documents[(document.tenant_id, document.document_id)] = document

    async def delete(self, tenant_id: str, document_id: str) -> bool:
        return self._documents.pop((tenant_id, document_id), None) is not None
