"""Port for storing documents."""

from typing import Protocol, runtime_checkable

from paimon.domain.entities import Document


@runtime_checkable
class DocumentRepository(Protocol):
    """Stores the normalized documents chunks were derived from.

    Separate from the vector store on purpose. The index holds chunks and exists
    to be searched; this holds the canonical document text and exists so a
    citation's character offsets can be resolved back to what they point at. A
    system that keeps only chunks can retrieve, but cannot show a claim in the
    context it was made in.
    """

    async def get(self, tenant_id: str, document_id: str) -> Document | None:
        """Return a document, or None if the tenant has no such document.

        Args:
            tenant_id: Owning organization.
            document_id: Document to load.
        """
        ...

    async def save(self, document: Document) -> None:
        """Insert or replace a document, keyed by tenant and document id.

        Args:
            document: The document to store.
        """
        ...

    async def delete(self, tenant_id: str, document_id: str) -> bool:
        """Remove a document.

        Args:
            tenant_id: Owning organization.
            document_id: Document to remove.

        Returns:
            Whether a document was removed.
        """
        ...
