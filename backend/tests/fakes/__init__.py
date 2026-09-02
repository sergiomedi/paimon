"""Reference implementations used by the contract suite and by use-case tests."""

from tests.fakes.chat import FakeChatModel
from tests.fakes.embedding import FakeEmbeddingModel
from tests.fakes.repository import InMemoryDocumentRepository
from tests.fakes.vector_store import InMemoryVectorStore

__all__ = [
    "FakeChatModel",
    "FakeEmbeddingModel",
    "InMemoryDocumentRepository",
    "InMemoryVectorStore",
]
