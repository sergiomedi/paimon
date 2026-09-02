"""The Azure AI Search adapter, run against the VectorStore contract.

The same assertions the pgvector adapter passes, against a stand-in service. See
tests/fakes/azure_search_service.py for exactly what that does and does not prove.
"""

import json

import httpx
import pytest

from paimon.domain.errors import RetrievalError
from paimon.domain.ports import (
    ChunkRecord,
    EmbeddingModel,
    NativeHybridSearch,
    SearchFilters,
    VectorStore,
)
from paimon.infrastructure.azure import ApiKeyCredential
from paimon.infrastructure.azure.search import AzureSearchConfig, AzureSearchStore, encode_key
from tests.contracts.vector_store import VectorStoreContract, chunk
from tests.fakes import FakeAzureSearchService, FakeEmbeddingModel

DIMENSIONS = 64
ENDPOINT = "https://search.example.net"


def store_for(
    service: FakeAzureSearchService,
    model: EmbeddingModel,
    *,
    semantic: str | None = None,
) -> AzureSearchStore:
    config = AzureSearchConfig(
        endpoint=ENDPOINT,
        index_name="chunks",
        embedding_model_id=model.model_id,
        dimensions=model.dimensions,
        semantic_configuration=semantic,
    )
    client = httpx.AsyncClient(transport=service.transport(), base_url=ENDPOINT)
    return AzureSearchStore(config, ApiKeyCredential("secret"), client)


class TestAzureSearchStore(VectorStoreContract):
    @pytest.fixture
    def embedding_model(self) -> EmbeddingModel:
        return FakeEmbeddingModel(dimensions=DIMENSIONS)

    @pytest.fixture
    def store(self, embedding_model: EmbeddingModel) -> VectorStore:
        return store_for(FakeAzureSearchService(), embedding_model)


class TestKeys:
    def test_a_chunk_id_becomes_a_legal_key(self) -> None:
        """Azure permits only letters, digits, underscore, dash and equals in a
        key, and chunk ids contain a colon."""
        key = encode_key("doc-1:7", "tenant-a")

        assert ":" not in key
        assert all(character.isalnum() or character in "-_=" for character in key)

    def test_two_tenants_with_the_same_chunk_id_get_different_keys(self) -> None:
        """A substitution that collapsed them would silently overwrite a chunk."""
        assert encode_key("doc-1:0", "tenant-a") != encode_key("doc-1:0", "tenant-b")


class TestNativeHybrid:
    async def test_the_store_advertises_native_fusion(self) -> None:
        """The capability flag of ADR-0003 as a type: the application defers to
        this backend's own ranker rather than duplicating it."""
        store = store_for(FakeAzureSearchService(), FakeEmbeddingModel(dimensions=DIMENSIONS))
        assert isinstance(store, NativeHybridSearch)

    async def test_a_hybrid_query_sends_both_signals_in_one_request(self) -> None:
        service = FakeAzureSearchService()
        model = FakeEmbeddingModel(dimensions=DIMENSIONS)
        store = store_for(service, model)
        (embedding,) = await model.embed_documents(["cordon the node"])
        await store.upsert([ChunkRecord(chunk=chunk("c1", "cordon the node"), embedding=embedding)])
        service.requests.clear()

        hits = await store.search_hybrid(
            "cordon", embedding, top_k=5, filters=SearchFilters(tenant_id="tenant-a")
        )

        payload = json.loads(service.requests[-1].content)
        assert payload["search"] == "cordon"
        assert payload["vectorQueries"][0]["fields"] == "embedding"
        assert [hit.retriever for hit in hits] == ["hybrid"]

    async def test_semantic_ranking_is_opt_in(self) -> None:
        """A capability pgvector has no equivalent for, exposed rather than
        silently applied."""
        plain = store_for(FakeAzureSearchService(), FakeEmbeddingModel(dimensions=DIMENSIONS))
        semantic = store_for(
            FakeAzureSearchService(),
            FakeEmbeddingModel(dimensions=DIMENSIONS),
            semantic="paimon-semantic",
        )

        assert plain.supports_semantic_ranking is False
        assert semantic.supports_semantic_ranking is True
        assert "semantic" in semantic.index_definition()


class TestIndexDefinition:
    def test_the_schema_is_declared_rather_than_discovered(self) -> None:
        """A schema found out from a failed write is a schema nobody reviewed."""
        store = store_for(FakeAzureSearchService(), FakeEmbeddingModel(dimensions=DIMENSIONS))
        definition = store.index_definition()

        vector_field = next(f for f in definition["fields"] if f["name"] == "embedding")
        assert vector_field["dimensions"] == DIMENSIONS
        assert definition["vectorSearch"]["algorithms"][0]["hnswParameters"]["metric"] == "cosine"

    async def test_ensure_index_sends_the_definition(self) -> None:
        service = FakeAzureSearchService()
        store = store_for(service, FakeEmbeddingModel(dimensions=DIMENSIONS))

        await store.ensure_index()

        assert service.index_definition is not None
        assert service.index_definition["name"] == "chunks"


class TestFailures:
    async def test_documents_rejected_inside_a_200_still_fail(self) -> None:
        """Azure reports per-document failures with a 200. A batch that half
        applied and returned success is how an index quietly loses chunks."""
        service = FakeAzureSearchService()
        model = FakeEmbeddingModel(dimensions=DIMENSIONS)
        store = store_for(service, model)
        target = chunk("c1", "cordon the node")
        service.reject_keys.add(encode_key(target.chunk_id, target.tenant_id))
        (embedding,) = await model.embed_documents([target.text])

        with pytest.raises(RetrievalError, match="rejected by azure ai search"):
            await store.upsert([ChunkRecord(chunk=target, embedding=embedding)])

    async def test_azures_error_code_is_reported(self) -> None:
        def refuse(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={"error": {"code": "AccessDenied"}})

        config = AzureSearchConfig(
            endpoint=ENDPOINT, index_name="chunks", embedding_model_id="m", dimensions=DIMENSIONS
        )
        client = httpx.AsyncClient(transport=httpx.MockTransport(refuse), base_url=ENDPOINT)
        store = AzureSearchStore(config, ApiKeyCredential("k"), client)

        with pytest.raises(RetrievalError, match="403 \\(AccessDenied\\)"):
            await store.search_lexical("x", top_k=5, filters=SearchFilters(tenant_id="t"))
