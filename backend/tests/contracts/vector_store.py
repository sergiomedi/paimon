"""Contract for the VectorStore port."""

import pytest

from paimon.domain.entities import Chunk
from paimon.domain.errors import IndexMismatchError
from paimon.domain.ports import ChunkRecord, EmbeddingModel, SearchFilters, VectorStore
from paimon.domain.value_objects import Embedding

TENANT = "tenant-a"
OTHER_TENANT = "tenant-b"


def chunk(
    chunk_id: str,
    text: str,
    *,
    tenant_id: str = TENANT,
    document_id: str = "doc-1",
    ordinal: int = 0,
) -> Chunk:
    """Build a chunk with plausible offsets for use in a contract test."""
    return Chunk(
        chunk_id=chunk_id,
        document_id=document_id,
        tenant_id=tenant_id,
        ordinal=ordinal,
        text=text,
        start_char=ordinal * 100,
        end_char=ordinal * 100 + len(text),
        token_count=max(len(text.split()), 1),
    )


class VectorStoreContract:
    """Every VectorStore adapter must pass these."""

    @pytest.fixture
    def store(self) -> VectorStore:
        """Supplied by the subclass, empty and ready to write to."""
        raise NotImplementedError

    @pytest.fixture
    def embedding_model(self) -> EmbeddingModel:
        """An embedding model matching the store's index descriptor."""
        raise NotImplementedError

    async def _write(
        self, store: VectorStore, embedding_model: EmbeddingModel, *chunks: Chunk
    ) -> None:
        embeddings = await embedding_model.embed_documents([item.text for item in chunks])
        await store.upsert(
            [
                ChunkRecord(chunk=item, embedding=embedding)
                for item, embedding in zip(chunks, embeddings, strict=True)
            ]
        )

    async def test_a_written_chunk_is_retrievable_by_meaning(
        self, store: VectorStore, embedding_model: EmbeddingModel
    ) -> None:
        await self._write(
            store,
            embedding_model,
            chunk("c1", "drain the node before rebooting it", ordinal=0),
            chunk("c2", "quarterly revenue by region", ordinal=1),
        )
        query = await embedding_model.embed_query("how do I drain a node")
        hits = await store.search_dense(query, top_k=2, filters=SearchFilters(tenant_id=TENANT))

        assert hits
        assert hits[0].chunk.chunk_id == "c1"

    async def test_a_written_chunk_is_retrievable_by_wording(
        self, store: VectorStore, embedding_model: EmbeddingModel
    ) -> None:
        await self._write(store, embedding_model, chunk("c1", "restart the ingest worker"))
        hits = await store.search_lexical(
            "ingest", top_k=5, filters=SearchFilters(tenant_id=TENANT)
        )

        assert [hit.chunk.chunk_id for hit in hits] == ["c1"]

    async def test_ranks_are_one_based_and_ordered(
        self, store: VectorStore, embedding_model: EmbeddingModel
    ) -> None:
        """Rank fusion depends on positions, so a store that leaves them unset or
        zero-based corrupts every fused result."""
        await self._write(
            store,
            embedding_model,
            chunk("c1", "drain the node before rebooting", ordinal=0),
            chunk("c2", "drain the queue before deploying", ordinal=1),
            chunk("c3", "unrelated marketing copy", ordinal=2),
        )
        query = await embedding_model.embed_query("drain the node")
        hits = await store.search_dense(query, top_k=3, filters=SearchFilters(tenant_id=TENANT))

        assert [hit.rank for hit in hits] == list(range(1, len(hits) + 1))
        assert [hit.score for hit in hits] == sorted((h.score for h in hits), reverse=True)

    async def test_top_k_is_respected(
        self, store: VectorStore, embedding_model: EmbeddingModel
    ) -> None:
        await self._write(
            store,
            embedding_model,
            *(chunk(f"c{i}", f"runbook step number {i}", ordinal=i) for i in range(5)),
        )
        query = await embedding_model.embed_query("runbook step")
        hits = await store.search_dense(query, top_k=2, filters=SearchFilters(tenant_id=TENANT))

        assert len(hits) == 2

    async def test_writing_the_same_chunk_id_replaces_it(
        self, store: VectorStore, embedding_model: EmbeddingModel
    ) -> None:
        """Ingestion must be repeatable: a partially failed run is simply re-run."""
        await self._write(store, embedding_model, chunk("c1", "first version of the step"))
        await self._write(store, embedding_model, chunk("c1", "second version of the step"))

        hits = await store.search_lexical(
            "version", top_k=10, filters=SearchFilters(tenant_id=TENANT)
        )
        assert len(hits) == 1
        assert "second" in hits[0].chunk.text

    async def test_another_tenant_sees_nothing(
        self, store: VectorStore, embedding_model: EmbeddingModel
    ) -> None:
        """Tenant isolation is the security boundary, so it is asserted rather
        than assumed of every backend."""
        await self._write(store, embedding_model, chunk("c1", "drain the node"))
        query = await embedding_model.embed_query("drain the node")

        assert (
            await store.search_dense(query, top_k=10, filters=SearchFilters(tenant_id=OTHER_TENANT))
            == []
        )
        assert (
            await store.search_lexical(
                "drain", top_k=10, filters=SearchFilters(tenant_id=OTHER_TENANT)
            )
            == []
        )

    async def test_results_can_be_restricted_to_documents(
        self, store: VectorStore, embedding_model: EmbeddingModel
    ) -> None:
        await self._write(
            store,
            embedding_model,
            chunk("c1", "drain the node", document_id="doc-1", ordinal=0),
            chunk("c2", "drain the node", document_id="doc-2", ordinal=1),
        )
        query = await embedding_model.embed_query("drain the node")
        hits = await store.search_dense(
            query,
            top_k=10,
            filters=SearchFilters(tenant_id=TENANT, document_ids=frozenset({"doc-2"})),
        )

        assert [hit.chunk.document_id for hit in hits] == ["doc-2"]

    async def test_deleting_a_document_removes_only_its_chunks(
        self, store: VectorStore, embedding_model: EmbeddingModel
    ) -> None:
        await self._write(
            store,
            embedding_model,
            chunk("c1", "step one of the runbook", document_id="doc-1", ordinal=0),
            chunk("c2", "step two of the runbook", document_id="doc-1", ordinal=1),
            chunk("c3", "an unrelated runbook", document_id="doc-2", ordinal=2),
        )
        removed = await store.delete_document(TENANT, "doc-1")

        assert removed == 2
        hits = await store.search_lexical(
            "runbook", top_k=10, filters=SearchFilters(tenant_id=TENANT)
        )
        assert [hit.chunk.document_id for hit in hits] == ["doc-2"]

    async def test_an_embedding_from_another_model_is_refused(
        self, store: VectorStore, embedding_model: EmbeddingModel
    ) -> None:
        """Two models in one index retrieves nonsense and never raises, so the
        mismatch is caught at the write instead."""
        foreign = Embedding(
            values=tuple(0.1 for _ in range(embedding_model.dimensions)),
            model_id="some-other-model",
        )
        with pytest.raises(IndexMismatchError):
            await store.upsert([ChunkRecord(chunk=chunk("c1", "text"), embedding=foreign)])

    async def test_an_embedding_of_the_wrong_size_is_refused(
        self, store: VectorStore, embedding_model: EmbeddingModel
    ) -> None:
        wrong_size = Embedding(
            values=tuple(0.1 for _ in range(embedding_model.dimensions + 1)),
            model_id=embedding_model.model_id,
        )
        with pytest.raises(IndexMismatchError):
            await store.upsert([ChunkRecord(chunk=chunk("c1", "text"), embedding=wrong_size)])

    async def test_a_refused_batch_writes_nothing(
        self, store: VectorStore, embedding_model: EmbeddingModel
    ) -> None:
        """A batch is validated in full before anything lands, so a rejection
        leaves no half-ingested document behind."""
        good = (await embedding_model.embed_documents(["a valid chunk"]))[0]
        bad = Embedding(values=(0.1, 0.2), model_id="some-other-model")

        with pytest.raises(IndexMismatchError):
            await store.upsert(
                [
                    ChunkRecord(chunk=chunk("c1", "a valid chunk", ordinal=0), embedding=good),
                    ChunkRecord(chunk=chunk("c2", "an invalid chunk", ordinal=1), embedding=bad),
                ]
            )

        hits = await store.search_lexical(
            "chunk", top_k=10, filters=SearchFilters(tenant_id=TENANT)
        )
        assert hits == []

    async def test_querying_with_a_foreign_embedding_is_refused(
        self, store: VectorStore, embedding_model: EmbeddingModel
    ) -> None:
        foreign = Embedding(
            values=tuple(0.1 for _ in range(embedding_model.dimensions)),
            model_id="some-other-model",
        )
        with pytest.raises(IndexMismatchError):
            await store.search_dense(foreign, top_k=5, filters=SearchFilters(tenant_id=TENANT))
