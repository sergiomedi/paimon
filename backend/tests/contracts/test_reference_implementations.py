"""The reference implementations, run against the contracts they define.

Running the fakes through the suite is what keeps the contracts executable: a
contract nothing satisfies is a wish list, and one that only a real adapter can
run cannot be checked in CI without a cloud account.
"""

import pytest

from paimon.domain.ports import (
    ChatModel,
    DocumentRepository,
    EmbeddingModel,
    IndexDescriptor,
    VectorStore,
)
from tests.contracts.chat_model import ChatModelContract
from tests.contracts.document_repository import DocumentRepositoryContract
from tests.contracts.embedding_model import EmbeddingModelContract
from tests.contracts.vector_store import VectorStoreContract
from tests.fakes import (
    FakeChatModel,
    FakeEmbeddingModel,
    InMemoryDocumentRepository,
    InMemoryVectorStore,
)

DIMENSIONS = 64


class TestFakeEmbeddingModel(EmbeddingModelContract):
    @pytest.fixture
    def embedding_model(self) -> EmbeddingModel:
        return FakeEmbeddingModel(dimensions=DIMENSIONS)


class TestFakeChatModel(ChatModelContract):
    @pytest.fixture
    def chat_model(self) -> ChatModel:
        return FakeChatModel()


class TestInMemoryVectorStore(VectorStoreContract):
    @pytest.fixture
    def embedding_model(self) -> EmbeddingModel:
        return FakeEmbeddingModel(dimensions=DIMENSIONS)

    @pytest.fixture
    def store(self, embedding_model: EmbeddingModel) -> VectorStore:
        return InMemoryVectorStore(
            IndexDescriptor(
                name="in-memory",
                embedding_model_id=embedding_model.model_id,
                dimensions=embedding_model.dimensions,
            )
        )


class TestInMemoryDocumentRepository(DocumentRepositoryContract):
    @pytest.fixture
    def repository(self) -> DocumentRepository:
        return InMemoryDocumentRepository()
