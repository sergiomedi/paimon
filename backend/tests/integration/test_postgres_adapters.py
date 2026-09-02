"""The PostgreSQL adapters, run against the same contracts as the fakes.

This is where the contract suite earns its keep: the assertions are identical to
the ones the in-memory implementations pass, so a difference in behaviour between
development and production shows up as a failing test rather than as a subtly
different answer.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from paimon.application.use_cases import RetrievalPolicy, RetrieveChunks
from paimon.domain.entities import Chunk
from paimon.domain.ports import (
    ChunkRecord,
    DocumentRepository,
    EmbeddingModel,
    IndexDescriptor,
    SearchFilters,
    VectorStore,
)
from paimon.infrastructure.persistence import PgVectorStore, PostgresDocumentRepository
from paimon.infrastructure.persistence.models.rag import EMBEDDING_DIMENSIONS
from tests.contracts.document_repository import DocumentRepositoryContract
from tests.contracts.vector_store import VectorStoreContract
from tests.fakes import FakeEmbeddingModel

pytestmark = pytest.mark.integration


@pytest.fixture
async def clean_tables(engine: AsyncEngine, migrated_database: None) -> None:
    """Start every test from an empty index."""
    async with engine.begin() as connection:
        await connection.execute(text("TRUNCATE TABLE chunks, documents"))


class TestPgVectorStore(VectorStoreContract):
    @pytest.fixture
    def embedding_model(self) -> EmbeddingModel:
        # The column is vector(1024), so the fake produces the real width and the
        # HNSW index is exercised at the size production uses.
        return FakeEmbeddingModel(dimensions=EMBEDDING_DIMENSIONS)

    @pytest.fixture
    def store(
        self, engine: AsyncEngine, embedding_model: EmbeddingModel, clean_tables: None
    ) -> VectorStore:
        return PgVectorStore(
            engine,
            IndexDescriptor(
                name="chunks",
                embedding_model_id=embedding_model.model_id,
                dimensions=embedding_model.dimensions,
            ),
        )


class TestPostgresDocumentRepository(DocumentRepositoryContract):
    @pytest.fixture
    def repository(self, engine: AsyncEngine, clean_tables: None) -> DocumentRepository:
        return PostgresDocumentRepository(engine)


class TestSchema:
    async def test_the_embedding_column_is_indexable_by_hnsw(
        self, engine: AsyncEngine, migrated_database: None
    ) -> None:
        """pgvector indexes the vector type with HNSW only up to 2000 dimensions,
        which is why the platform fixes embeddings at 1024 (ADR-0011)."""
        assert EMBEDDING_DIMENSIONS <= 2000

        async with engine.connect() as connection:
            result = await connection.execute(
                text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_chunks_embedding_hnsw'")
            )
            definition = result.scalar_one()

        assert "hnsw" in definition
        assert "vector_cosine_ops" in definition

    async def test_the_search_vector_is_generated_by_the_database(
        self, engine: AsyncEngine, migrated_database: None
    ) -> None:
        """A derived column the application maintains drifts the moment one write
        path forgets it."""
        async with engine.connect() as connection:
            result = await connection.execute(
                text(
                    "SELECT is_generated FROM information_schema.columns "
                    "WHERE table_name = 'chunks' AND column_name = 'search_vector'"
                )
            )
            assert result.scalar_one() == "ALWAYS"

    async def test_headings_are_searchable(
        self, engine: AsyncEngine, migrated_database: None, clean_tables: None
    ) -> None:
        """A section title is often the only place a term appears, so the
        generated tsvector includes the heading path as well as the body."""
        store = PgVectorStore(
            engine,
            IndexDescriptor(
                name="chunks", embedding_model_id="fake-embed-v1", dimensions=EMBEDDING_DIMENSIONS
            ),
        )
        model = FakeEmbeddingModel(dimensions=EMBEDDING_DIMENSIONS)
        chunk = Chunk(
            chunk_id="c1",
            document_id="doc-1",
            tenant_id="tenant-a",
            ordinal=0,
            text="Cordon it, then wait.",
            start_char=0,
            end_char=21,
            token_count=5,
            heading_path=("Runbooks", "Decommissioning"),
        )
        (embedding,) = await model.embed_documents([chunk.text])
        await store.upsert([ChunkRecord(chunk=chunk, embedding=embedding)])

        hits = await store.search_lexical(
            "decommissioning", top_k=5, filters=SearchFilters(tenant_id="tenant-a")
        )
        assert [hit.chunk.chunk_id for hit in hits] == ["c1"]


class TestHybridRetrievalAgainstPostgres:
    """The retrieval use case over real BM25 ranking and a real HNSW index.

    The unit tests pin the fusion algorithm; this pins the plumbing — that both
    of PostgreSQL's retrievers actually run, return one-based ranks, and fuse
    into a single ordering.
    """

    @pytest.fixture
    def store(
        self, engine: AsyncEngine, embedding_model: EmbeddingModel, clean_tables: None
    ) -> PgVectorStore:
        return PgVectorStore(
            engine,
            IndexDescriptor(
                name="chunks",
                embedding_model_id=embedding_model.model_id,
                dimensions=embedding_model.dimensions,
            ),
        )

    @pytest.fixture
    def embedding_model(self) -> EmbeddingModel:
        return FakeEmbeddingModel(dimensions=EMBEDDING_DIMENSIONS)

    async def _index(self, store: PgVectorStore, model: EmbeddingModel, *texts: str) -> None:
        chunks = [
            Chunk(
                chunk_id=f"c{index}",
                document_id="doc-1",
                tenant_id="tenant-a",
                ordinal=index,
                text=text,
                start_char=index * 200,
                end_char=index * 200 + len(text),
                token_count=max(len(text.split()), 1),
            )
            for index, text in enumerate(texts)
        ]
        embeddings = await model.embed_documents([chunk.text for chunk in chunks])
        await store.upsert(
            [
                ChunkRecord(chunk=chunk, embedding=embedding)
                for chunk, embedding in zip(chunks, embeddings, strict=True)
            ]
        )

    async def test_both_retrievers_contribute_to_the_fused_result(
        self, store: PgVectorStore, embedding_model: EmbeddingModel
    ) -> None:
        await self._index(
            store,
            embedding_model,
            "Cordon the node before rebooting it.",
            "Eviction stalls without a pod disruption budget.",
            "Quarterly revenue by region and product line.",
        )

        result = await RetrieveChunks(
            store, embedding_model, RetrievalPolicy(top_k=3, candidates_per_retriever=10)
        )("cordon the node", SearchFilters(tenant_id="tenant-a"))

        assert result.strategy == "fused"
        assert result.hits
        assert result.hits[0].chunk.chunk_id == "c0"
        assert [hit.rank for hit in result.hits] == list(range(1, len(result.hits) + 1))
        assert "lexical" in result.hits[0].retrievers
        assert "dense" in result.hits[0].retrievers

    async def test_retrieval_is_isolated_by_tenant(
        self, store: PgVectorStore, embedding_model: EmbeddingModel
    ) -> None:
        await self._index(store, embedding_model, "Cordon the node before rebooting it.")

        result = await RetrieveChunks(store, embedding_model)(
            "cordon", SearchFilters(tenant_id="tenant-b")
        )
        assert result.hits == ()
