"""PostgreSQL adapter for the DocumentRepository port."""

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from paimon.domain.entities import Document
from paimon.infrastructure.persistence.models import DocumentRow


class PostgresDocumentRepository:
    """Stores normalized documents in PostgreSQL."""

    def __init__(self, engine: AsyncEngine) -> None:
        """Initialise the repository.

        Args:
            engine: Engine whose pool the repository borrows connections from.
        """
        self._engine = engine

    async def get(self, tenant_id: str, document_id: str) -> Document | None:
        """Return a document, or None if the tenant has no such document."""
        statement = select(DocumentRow).where(
            DocumentRow.tenant_id == tenant_id,
            DocumentRow.document_id == document_id,
        )
        async with self._engine.connect() as connection:
            row = (await connection.execute(statement)).mappings().one_or_none()
        if row is None:
            return None
        return Document(
            document_id=row["document_id"],
            tenant_id=row["tenant_id"],
            source_uri=row["source_uri"],
            title=row["title"],
            text=row["text"],
            content_hash=row["content_hash"],
            media_type=row["media_type"],
            metadata=dict(row["doc_metadata"]),
        )

    async def save(self, document: Document) -> None:
        """Insert or replace a document.

        Upsert rather than delete-and-insert: re-ingestion is routine, and a
        window in which the document does not exist would let a concurrent
        citation lookup fail on text that was never actually removed.
        """
        values = {
            "tenant_id": document.tenant_id,
            "document_id": document.document_id,
            "source_uri": document.source_uri,
            "title": document.title,
            "text": document.text,
            "content_hash": document.content_hash,
            "media_type": document.media_type,
            "doc_metadata": dict(document.metadata),
        }
        statement = insert(DocumentRow).values(**values)
        mutable = {
            key: statement.excluded[key]
            for key in values
            if key not in {"tenant_id", "document_id"}
        }
        statement = statement.on_conflict_do_update(
            index_elements=[DocumentRow.tenant_id, DocumentRow.document_id],
            set_={**mutable, "updated_at": func.now()},
        )
        async with self._engine.begin() as connection:
            await connection.execute(statement)

    async def delete(self, tenant_id: str, document_id: str) -> bool:
        """Remove a document, reporting whether one was there."""
        statement = delete(DocumentRow).where(
            DocumentRow.tenant_id == tenant_id,
            DocumentRow.document_id == document_id,
        )
        async with self._engine.begin() as connection:
            result = await connection.execute(statement)
        return result.rowcount > 0
