"""Contract for the EmbeddingModel port."""

import pytest

from paimon.domain.ports import EmbeddingModel
from paimon.domain.value_objects import cosine_similarity


class EmbeddingModelContract:
    """Every EmbeddingModel adapter must pass these."""

    @pytest.fixture
    def embedding_model(self) -> EmbeddingModel:
        """Supplied by the subclass."""
        raise NotImplementedError

    async def test_it_declares_the_dimensions_it_produces(
        self, embedding_model: EmbeddingModel
    ) -> None:
        """The declared dimensionality is what the index is built on; if the model
        disagrees with itself, every write is a mismatch waiting to happen."""
        (embedding,) = await embedding_model.embed_documents(["a runbook paragraph"])
        assert embedding.dimensions == embedding_model.dimensions

    async def test_every_embedding_records_its_model(self, embedding_model: EmbeddingModel) -> None:
        (embedding,) = await embedding_model.embed_documents(["text"])
        assert embedding.model_id == embedding_model.model_id

    async def test_it_is_deterministic(self, embedding_model: EmbeddingModel) -> None:
        """Re-ingesting an unchanged document must not churn the index."""
        first = await embedding_model.embed_query("restart the ingest worker")
        second = await embedding_model.embed_query("restart the ingest worker")
        assert first.values == second.values

    async def test_a_batch_preserves_order_and_length(
        self, embedding_model: EmbeddingModel
    ) -> None:
        """Results are matched to inputs by position, so a reordering silently
        attaches every embedding to the wrong chunk."""
        texts = ["alpha document", "beta document", "gamma document"]
        batched = await embedding_model.embed_documents(texts)
        assert len(batched) == len(texts)
        for text, embedded in zip(texts, batched, strict=True):
            assert embedded.values == (await embedding_model.embed_query(text)).values

    async def test_an_empty_batch_returns_nothing(self, embedding_model: EmbeddingModel) -> None:
        """A document that chunks to nothing must not become a provider call."""
        assert await embedding_model.embed_documents([]) == []

    async def test_a_query_embedding_matches_the_index_shape(
        self, embedding_model: EmbeddingModel
    ) -> None:
        embedding = await embedding_model.embed_query("how do I drain a node")
        assert embedding.dimensions == embedding_model.dimensions
        assert embedding.model_id == embedding_model.model_id

    async def test_shared_wording_is_closer_than_unrelated_wording(
        self, embedding_model: EmbeddingModel
    ) -> None:
        """The weakest useful quality invariant: an embedding that fails this is
        not producing a similarity space at all."""
        subject = await embedding_model.embed_query("restart the ingest worker")
        related = await embedding_model.embed_query("restart the ingest worker process")
        unrelated = await embedding_model.embed_query("quarterly revenue by region")

        assert cosine_similarity(subject, related) > cosine_similarity(subject, unrelated)
