"""A corpus and a wiring harness shared by the agent tests."""

from collections.abc import Sequence

from tests.fakes import (
    FakeChatModel,
    FakeEmbeddingModel,
    InMemoryCheckpointer,
    InMemoryDocumentRepository,
    InMemoryVectorStore,
)

from paimon.application.use_cases.retrieve_chunks import RetrieveChunks
from paimon.domain.entities import Chunk, Document
from paimon.domain.errors import EmbeddingError
from paimon.domain.ports import ChunkRecord, EmbeddingModel, IndexDescriptor
from paimon.domain.value_objects import Embedding
from paimon.infrastructure.orchestration import LangGraphWorkflow
from paimon.infrastructure.tokenization import HeuristicTokenCounter

TENANT = "tenant-a"
DIMENSIONS = 64

RUNBOOK = """# Node maintenance

Cordon the node first so the scheduler stops placing new pods on it. Eviction
stalls indefinitely when a disruption budget cannot be satisfied.
"""

POSTMORTEM = """# INC-2451

A drain stalled for forty minutes because a disruption budget could not be met.
Detection was slow; nobody owned the alert.
"""


def chunk(chunk_id: str, document_id: str, text: str) -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        document_id=document_id,
        tenant_id=TENANT,
        ordinal=0,
        text=text,
        start_char=0,
        end_char=len(text),
        token_count=max(len(text.split()), 1),
    )


def document(document_id: str, text: str) -> Document:
    return Document(
        document_id=document_id,
        tenant_id=TENANT,
        source_uri=f"https://example.test/{document_id}",
        title=document_id,
        text=text,
        content_hash=f"hash-{document_id}",
        media_type="text/markdown",
    )


class UnreachableEmbeddingModel:
    """An embedding model whose provider is down.

    Not a contrived case: it is what a local Ollama that is not running, or an
    Azure endpoint behind a network blip, looks like from inside a node.
    """

    def __init__(self, dimensions: int = DIMENSIONS) -> None:
        self.dimensions = dimensions
        self.model_id = "unreachable"

    async def embed_documents(self, texts: Sequence[str]) -> list[Embedding]:
        msg = "embedding provider unreachable: All connection attempts failed"
        raise EmbeddingError(msg)

    async def embed_query(self, text: str) -> Embedding:
        msg = "embedding provider unreachable: All connection attempts failed"
        raise EmbeddingError(msg)


class Harness:
    """In-memory everything, wired the way the composition root wires it."""

    def __init__(
        self, answer: str = "Cordon the node first [1].", *, reachable: bool = True
    ) -> None:
        self.embedding_model: EmbeddingModel = (
            FakeEmbeddingModel(dimensions=DIMENSIONS) if reachable else UnreachableEmbeddingModel()
        )
        self.store = InMemoryVectorStore(
            IndexDescriptor(
                name="in-memory",
                embedding_model_id=self.embedding_model.model_id,
                dimensions=DIMENSIONS,
            )
        )
        self.repository = InMemoryDocumentRepository()
        self.chat_model = FakeChatModel(answer=answer)
        self.checkpointer = InMemoryCheckpointer()
        self.retrieve = RetrieveChunks(self.store, self.embedding_model)
        self.token_counter = HeuristicTokenCounter()

    async def index(self) -> None:
        chunks = [
            chunk("c-run", "runbook", RUNBOOK),
            chunk("c-inc", "incident", POSTMORTEM),
        ]
        embeddings = await self.embedding_model.embed_documents([item.text for item in chunks])
        await self.store.upsert(
            [
                ChunkRecord(chunk=item, embedding=embedding)
                for item, embedding in zip(chunks, embeddings, strict=True)
            ]
        )
        await self.repository.save(document("runbook", RUNBOOK))
        await self.repository.save(document("incident", POSTMORTEM))

    def workflow(self, spec: object) -> LangGraphWorkflow:
        return LangGraphWorkflow(spec, self.checkpointer)  # type: ignore[arg-type]
