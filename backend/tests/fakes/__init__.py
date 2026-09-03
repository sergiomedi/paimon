"""Reference implementations used by the contract suite and by use-case tests."""

from tests.fakes.azure_search_service import FakeAzureSearchService
from tests.fakes.chat import FakeChatModel
from tests.fakes.checkpointer import InMemoryCheckpointer
from tests.fakes.embedding import FakeEmbeddingModel
from tests.fakes.repository import InMemoryDocumentRepository
from tests.fakes.vector_store import InMemoryHybridVectorStore, InMemoryVectorStore

__all__ = [
    "FakeAzureSearchService",
    "FakeChatModel",
    "FakeEmbeddingModel",
    "InMemoryCheckpointer",
    "InMemoryDocumentRepository",
    "InMemoryHybridVectorStore",
    "InMemoryVectorStore",
]
