"""Tests for the retrieval use case."""

import pytest

from paimon.application.use_cases import RetrievalPolicy, RetrieveChunks
from paimon.domain.entities import Chunk
from paimon.domain.ports import ChunkRecord, IndexDescriptor, SearchFilters
from tests.fakes import FakeEmbeddingModel, InMemoryHybridVectorStore, InMemoryVectorStore

TENANT = "tenant-1"
DIMENSIONS = 64


def chunk(chunk_id: str, text: str, *, tenant_id: str = TENANT, ordinal: int = 0) -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        document_id="doc-1",
        tenant_id=tenant_id,
        ordinal=ordinal,
        text=text,
        start_char=ordinal * 200,
        end_char=ordinal * 200 + len(text),
        token_count=max(len(text.split()), 1),
    )


@pytest.fixture
def embedding_model() -> FakeEmbeddingModel:
    return FakeEmbeddingModel(dimensions=DIMENSIONS)


def descriptor(model: FakeEmbeddingModel) -> IndexDescriptor:
    return IndexDescriptor(
        name="test", embedding_model_id=model.model_id, dimensions=model.dimensions
    )


@pytest.fixture
def store(embedding_model: FakeEmbeddingModel) -> InMemoryVectorStore:
    return InMemoryVectorStore(descriptor(embedding_model))


async def fill(store: InMemoryVectorStore, model: FakeEmbeddingModel, *chunks: Chunk) -> None:
    embeddings = await model.embed_documents([item.text for item in chunks])
    await store.upsert(
        [
            ChunkRecord(chunk=item, embedding=embedding)
            for item, embedding in zip(chunks, embeddings, strict=True)
        ]
    )


class TestHybridBehaviour:
    async def test_it_runs_both_retrievers_and_fuses(
        self, store: InMemoryVectorStore, embedding_model: FakeEmbeddingModel
    ) -> None:
        await fill(store, embedding_model, chunk("c1", "cordon the node before rebooting"))
        result = await RetrieveChunks(store, embedding_model)(
            "cordon the node", SearchFilters(tenant_id=TENANT)
        )

        assert result.strategy == "fused"
        assert store.dense_calls
        assert store.lexical_calls

    async def test_a_chunk_only_one_retriever_finds_still_surfaces(
        self, store: InMemoryVectorStore, embedding_model: FakeEmbeddingModel
    ) -> None:
        """The case hybrid retrieval exists for. The lexical retriever matches the
        exact token; the dense one matches the paraphrase."""
        await fill(
            store,
            embedding_model,
            chunk("exact", "error ORA-01555 snapshot too old", ordinal=0),
            chunk("paraphrase", "the query failed because the snapshot expired", ordinal=1),
        )
        result = await RetrieveChunks(store, embedding_model)(
            "ORA-01555 snapshot expired", SearchFilters(tenant_id=TENANT)
        )

        assert {hit.chunk.chunk_id for hit in result.hits} == {"exact", "paraphrase"}

    async def test_each_hit_records_which_retrievers_found_it(
        self, store: InMemoryVectorStore, embedding_model: FakeEmbeddingModel
    ) -> None:
        await fill(store, embedding_model, chunk("c1", "cordon the node"))
        result = await RetrieveChunks(store, embedding_model)(
            "cordon the node", SearchFilters(tenant_id=TENANT)
        )

        assert set(result.hits[0].retrievers) == {"dense", "lexical"}


class TestCandidateDepth:
    async def test_it_gathers_more_candidates_than_it_returns(
        self, store: InMemoryVectorStore, embedding_model: FakeEmbeddingModel
    ) -> None:
        """A chunk ranked eighth by one retriever and unseen by the other can win
        once fused; fusing only the top few would discard it first."""
        await fill(store, embedding_model, chunk("c1", "cordon the node"))
        policy = RetrievalPolicy(top_k=3, candidates_per_retriever=25)

        result = await RetrieveChunks(store, embedding_model, policy)(
            "cordon", SearchFilters(tenant_id=TENANT)
        )

        assert store.dense_calls == [25]
        assert store.lexical_calls == [("cordon", 25)]
        assert len(result.hits) <= 3

    async def test_returning_more_than_is_gathered_is_refused(self) -> None:
        with pytest.raises(ValueError, match="cannot be smaller than top_k"):
            RetrievalPolicy(top_k=20, candidates_per_retriever=5)

    async def test_a_non_positive_top_k_is_refused(self) -> None:
        with pytest.raises(ValueError, match="top_k must be positive"):
            RetrievalPolicy(top_k=0)


class TestNativeHybrid:
    async def test_a_store_that_fuses_natively_is_deferred_to(
        self, embedding_model: FakeEmbeddingModel
    ) -> None:
        """Azure AI Search fuses with information this layer does not have, so
        duplicating the work here would be both wasteful and worse."""
        store = InMemoryHybridVectorStore(descriptor(embedding_model))
        await fill(store, embedding_model, chunk("c1", "cordon the node"))

        result = await RetrieveChunks(store, embedding_model)(
            "cordon", SearchFilters(tenant_id=TENANT)
        )

        assert result.strategy == "native_hybrid"
        assert store.hybrid_calls == [("cordon", 8)]
        assert store.lexical_calls == []

    async def test_natively_fused_hits_have_the_same_shape(
        self, embedding_model: FakeEmbeddingModel
    ) -> None:
        """The caller must not have to branch on which backend answered."""
        store = InMemoryHybridVectorStore(descriptor(embedding_model))
        await fill(store, embedding_model, chunk("c1", "cordon the node"))

        result = await RetrieveChunks(store, embedding_model)(
            "cordon", SearchFilters(tenant_id=TENANT)
        )
        hit = result.hits[0]

        assert hit.rank == 1
        assert hit.retrievers == ("hybrid",)


class TestBoundaries:
    @pytest.mark.parametrize("query", ["", "   ", "\n\t"])
    async def test_an_empty_query_retrieves_nothing_without_calling_the_model(
        self,
        store: InMemoryVectorStore,
        embedding_model: FakeEmbeddingModel,
        query: str,
    ) -> None:
        result = await RetrieveChunks(store, embedding_model)(
            query, SearchFilters(tenant_id=TENANT)
        )

        assert result.hits == ()
        assert embedding_model.query_calls == []

    async def test_an_empty_index_retrieves_nothing(
        self, store: InMemoryVectorStore, embedding_model: FakeEmbeddingModel
    ) -> None:
        result = await RetrieveChunks(store, embedding_model)(
            "anything", SearchFilters(tenant_id=TENANT)
        )
        assert result.hits == ()

    async def test_another_tenant_retrieves_nothing(
        self, store: InMemoryVectorStore, embedding_model: FakeEmbeddingModel
    ) -> None:
        await fill(store, embedding_model, chunk("c1", "cordon the node"))
        result = await RetrieveChunks(store, embedding_model)(
            "cordon", SearchFilters(tenant_id="tenant-2")
        )

        assert result.hits == ()
