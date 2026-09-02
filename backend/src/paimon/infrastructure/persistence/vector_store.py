"""PostgreSQL and pgvector adapter for the VectorStore port."""

from collections.abc import Sequence
from typing import Any

from sqlalchemy import Row, delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.sql import Select

from paimon.domain.entities import Chunk
from paimon.domain.errors import IndexMismatchError
from paimon.domain.ports import ChunkRecord, IndexDescriptor, SearchFilters, SearchHit
from paimon.domain.value_objects import Embedding
from paimon.infrastructure.persistence.models import ChunkRow

# Matches the text search configuration the generated search_vector column uses.
# A query parsed with a different configuration stems differently and quietly
# stops matching its own index.
_TEXT_SEARCH_CONFIG = "english"


class PgVectorStore:
    """Stores chunks in PostgreSQL and retrieves them two ways.

    Dense retrieval uses cosine distance over the HNSW index; lexical retrieval
    uses PostgreSQL full-text ranking over a generated tsvector. Both read the
    same rows, so a hit found by one is the same object as a hit found by the
    other and rank fusion has something coherent to fuse.
    """

    def __init__(self, engine: AsyncEngine, descriptor: IndexDescriptor) -> None:
        """Initialise the store.

        Args:
            engine: Engine whose pool the store borrows connections from.
            descriptor: The index this store writes to, including the embedding
                model and dimensionality it accepts.
        """
        self._engine = engine
        self._descriptor = descriptor

    @property
    def descriptor(self) -> IndexDescriptor:
        """The index this store writes to and reads from."""
        return self._descriptor

    def _reject_mismatch(self, embedding: Embedding) -> None:
        if embedding.model_id != self._descriptor.embedding_model_id:
            msg = (
                f"index '{self._descriptor.name}' holds embeddings from "
                f"'{self._descriptor.embedding_model_id}', got '{embedding.model_id}'"
            )
            raise IndexMismatchError(msg)
        if embedding.dimensions != self._descriptor.dimensions:
            msg = (
                f"index '{self._descriptor.name}' has {self._descriptor.dimensions} "
                f"dimensions, got {embedding.dimensions}"
            )
            raise IndexMismatchError(msg)

    async def upsert(self, records: Sequence[ChunkRecord]) -> None:
        """Insert or replace chunks, keyed by tenant and chunk id."""
        # Validated in full before anything is written, so a rejected batch leaves
        # the index exactly as it was.
        for record in records:
            self._reject_mismatch(record.embedding)
        if not records:
            return

        rows = [
            {
                "tenant_id": record.chunk.tenant_id,
                "chunk_id": record.chunk.chunk_id,
                "document_id": record.chunk.document_id,
                "ordinal": record.chunk.ordinal,
                "text": record.chunk.text,
                "heading_path": list(record.chunk.heading_path),
                "start_char": record.chunk.start_char,
                "end_char": record.chunk.end_char,
                "token_count": record.chunk.token_count,
                "embedding": list(record.embedding.values),
                "embedding_model": record.embedding.model_id,
            }
            for record in records
        ]
        statement = insert(ChunkRow).values(rows)
        statement = statement.on_conflict_do_update(
            index_elements=[ChunkRow.tenant_id, ChunkRow.chunk_id],
            set_={
                key: statement.excluded[key]
                for key in rows[0]
                if key not in {"tenant_id", "chunk_id"}
            },
        )
        async with self._engine.begin() as connection:
            await connection.execute(statement)

    async def delete_document(self, tenant_id: str, document_id: str) -> int:
        """Remove every chunk of a document, reporting how many went."""
        statement = delete(ChunkRow).where(
            ChunkRow.tenant_id == tenant_id,
            ChunkRow.document_id == document_id,
        )
        async with self._engine.begin() as connection:
            result = await connection.execute(statement)
        return int(result.rowcount)

    def _restrict(self, statement: Select[Any], filters: SearchFilters) -> Select[Any]:
        statement = statement.where(ChunkRow.tenant_id == filters.tenant_id)
        if filters.document_ids is not None:
            statement = statement.where(ChunkRow.document_id.in_(sorted(filters.document_ids)))
        return statement

    @staticmethod
    def _to_chunk(row: Row[Any]) -> Chunk:
        mapping = row._mapping
        return Chunk(
            chunk_id=str(mapping["chunk_id"]),
            document_id=str(mapping["document_id"]),
            tenant_id=str(mapping["tenant_id"]),
            ordinal=int(mapping["ordinal"]),
            text=str(mapping["text"]),
            start_char=int(mapping["start_char"]),
            end_char=int(mapping["end_char"]),
            token_count=int(mapping["token_count"]),
            heading_path=tuple(mapping["heading_path"]),
        )

    _COLUMNS = (
        ChunkRow.tenant_id,
        ChunkRow.chunk_id,
        ChunkRow.document_id,
        ChunkRow.ordinal,
        ChunkRow.text,
        ChunkRow.heading_path,
        ChunkRow.start_char,
        ChunkRow.end_char,
        ChunkRow.token_count,
    )

    async def search_dense(
        self, embedding: Embedding, *, top_k: int, filters: SearchFilters
    ) -> list[SearchHit]:
        """Retrieve by cosine similarity, using the HNSW index."""
        self._reject_mismatch(embedding)
        vector = list(embedding.values)
        # cosine_distance is 1 - cosine similarity, so ascending distance is
        # descending similarity, and the score is converted back for the caller.
        distance = ChunkRow.embedding.cosine_distance(vector).label("distance")
        statement = self._restrict(select(*self._COLUMNS, distance), filters)
        statement = statement.order_by(distance).limit(top_k)

        async with self._engine.connect() as connection:
            rows = (await connection.execute(statement)).all()

        return [
            SearchHit(
                chunk=self._to_chunk(row),
                score=1.0 - float(row._mapping["distance"]),
                rank=position,
                retriever="dense",
            )
            for position, row in enumerate(rows, start=1)
        ]

    async def search_lexical(
        self, query: str, *, top_k: int, filters: SearchFilters
    ) -> list[SearchHit]:
        """Retrieve by full-text ranking, using the GIN index."""
        if not query.strip():
            return []
        parsed = func.websearch_to_tsquery(_TEXT_SEARCH_CONFIG, query)
        rank = func.ts_rank_cd(ChunkRow.search_vector, parsed).label("rank_score")
        statement = self._restrict(select(*self._COLUMNS, rank), filters)
        statement = statement.where(ChunkRow.search_vector.op("@@")(parsed))
        statement = statement.order_by(rank.desc()).limit(top_k)

        async with self._engine.connect() as connection:
            rows = (await connection.execute(statement)).all()

        return [
            SearchHit(
                chunk=self._to_chunk(row),
                score=float(row._mapping["rank_score"]),
                rank=position,
                retriever="lexical",
            )
            for position, row in enumerate(rows, start=1)
        ]
