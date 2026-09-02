"""End-to-end tests for ingestion and grounded answering.

The use cases are wired to reference implementations so the whole HTTP path —
auth, request parsing, the use case, error translation and the response shape —
is exercised without a database or a model server.
"""

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from paimon.application.use_cases import AnswerQuestion, IngestDocument, RetrieveChunks
from paimon.domain.ports import IndexDescriptor
from paimon.infrastructure.identity import DevIdentityProvider
from paimon.infrastructure.parsing import MarkdownParser
from paimon.infrastructure.tokenization import HeuristicTokenCounter
from paimon.interfaces.api.dependencies import get_answer_question, get_ingest_document
from paimon.rag.chunking import Chunker, ChunkingPolicy
from tests.fakes import (
    FakeChatModel,
    FakeEmbeddingModel,
    InMemoryDocumentRepository,
    InMemoryVectorStore,
)

TENANT = "tenant-1"
RUNBOOK = """# Node maintenance

Nodes are drained before any kernel upgrade, without exception.

## Draining

Cordon the node first so the scheduler stops placing new pods on it.
"""


class Backend:
    """Reference implementations shared by the ingestion and answering paths."""

    def __init__(self, answer: str = "Cordon the node first [1].") -> None:
        self.embedding_model = FakeEmbeddingModel(dimensions=64)
        self.chat_model = FakeChatModel(answer=answer)
        self.repository = InMemoryDocumentRepository()
        self.store = InMemoryVectorStore(
            IndexDescriptor(
                name="test", embedding_model_id=self.embedding_model.model_id, dimensions=64
            )
        )

    def ingest(self) -> IngestDocument:
        return IngestDocument(
            parser=MarkdownParser(),
            repository=self.repository,
            store=self.store,
            embedding_model=self.embedding_model,
            chunker=Chunker(
                ChunkingPolicy(max_tokens=80, overlap_tokens=10, min_tokens=5),
                HeuristicTokenCounter(),
            ),
        )

    def answer(self) -> AnswerQuestion:
        return AnswerQuestion(
            retrieve=RetrieveChunks(self.store, self.embedding_model),
            chat_model=self.chat_model,
            repository=self.repository,
            token_counter=HeuristicTokenCounter(),
        )


@pytest.fixture
def backend(app: FastAPI) -> Iterator[Backend]:
    instance = Backend()
    app.dependency_overrides[get_ingest_document] = instance.ingest
    app.dependency_overrides[get_answer_question] = instance.answer
    yield instance
    app.dependency_overrides.clear()


@pytest.fixture
def auth(dev_identity_provider: DevIdentityProvider) -> dict[str, str]:
    token = dev_identity_provider.issue(subject="user-1", tenant_id=TENANT)
    return {"Authorization": f"Bearer {token}"}


def document_body(content: str = RUNBOOK) -> dict[str, object]:
    return {
        "source_uri": "https://example.test/runbook.md",
        "content": content,
        "media_type": "text/markdown",
    }


class TestIngestion:
    async def test_a_document_is_indexed(
        self, client: AsyncClient, backend: Backend, auth: dict[str, str]
    ) -> None:
        response = await client.put("/api/v1/documents/runbook", json=document_body(), headers=auth)

        assert response.status_code == 200
        body = response.json()
        assert body["document_id"] == "runbook"
        assert body["chunks_indexed"] > 0
        assert body["unchanged"] is False

    async def test_sending_it_again_does_no_work(
        self, client: AsyncClient, backend: Backend, auth: dict[str, str]
    ) -> None:
        """PUT is idempotent by document id, and unchanged content costs a hash
        comparison rather than a round of embeddings."""
        await client.put("/api/v1/documents/runbook", json=document_body(), headers=auth)
        batches = len(backend.embedding_model.document_batches)

        response = await client.put("/api/v1/documents/runbook", json=document_body(), headers=auth)

        assert response.json()["unchanged"] is True
        assert len(backend.embedding_model.document_batches) == batches

    async def test_an_unsupported_media_type_is_415(
        self, client: AsyncClient, backend: Backend, auth: dict[str, str]
    ) -> None:
        response = await client.put(
            "/api/v1/documents/spec",
            json={**document_body("%PDF-1.7"), "media_type": "application/pdf"},
            headers=auth,
        )
        assert response.status_code == 415

    async def test_a_document_that_chunks_to_nothing_is_400(
        self, client: AsyncClient, backend: Backend, auth: dict[str, str]
    ) -> None:
        response = await client.put(
            "/api/v1/documents/empty", json=document_body("# Heading only\n"), headers=auth
        )
        assert response.status_code == 400

    async def test_ingestion_requires_authentication(
        self, client: AsyncClient, backend: Backend
    ) -> None:
        response = await client.put("/api/v1/documents/runbook", json=document_body())
        assert response.status_code == 401


class TestAnswering:
    async def test_an_answer_carries_resolvable_citations(
        self, client: AsyncClient, backend: Backend, auth: dict[str, str]
    ) -> None:
        await client.put("/api/v1/documents/runbook", json=document_body(), headers=auth)

        response = await client.post(
            "/api/v1/answers", json={"question": "how do I drain a node"}, headers=auth
        )

        assert response.status_code == 200
        body = response.json()
        assert body["grounded"] is True
        citation = body["citations"][0]
        assert citation["document_id"] == "runbook"
        assert citation["end_char"] > citation["start_char"]
        assert citation["quote"]

    async def test_a_citation_resolves_against_the_stored_document(
        self, client: AsyncClient, backend: Backend, auth: dict[str, str]
    ) -> None:
        """The claim the whole design rests on, asserted end to end."""
        await client.put("/api/v1/documents/runbook", json=document_body(), headers=auth)
        response = await client.post(
            "/api/v1/answers", json={"question": "how do I drain a node"}, headers=auth
        )
        citation = response.json()["citations"][0]

        stored = await backend.repository.get(TENANT, "runbook")
        assert stored is not None
        assert stored.text[citation["start_char"] : citation["end_char"]] == citation["quote"]

    async def test_nothing_indexed_is_an_honest_refusal_not_an_error(
        self, client: AsyncClient, backend: Backend, auth: dict[str, str]
    ) -> None:
        """A 200 with grounded false is a normal outcome. Treating it as a failure
        would push callers to retry until they got an ungrounded answer."""
        response = await client.post("/api/v1/answers", json={"question": "anything"}, headers=auth)

        assert response.status_code == 200
        body = response.json()
        assert body["grounded"] is False
        assert body["citations"] == []
        assert backend.chat_model.calls == []

    async def test_answering_is_isolated_by_tenant(
        self,
        client: AsyncClient,
        backend: Backend,
        auth: dict[str, str],
        dev_identity_provider: DevIdentityProvider,
    ) -> None:
        await client.put("/api/v1/documents/runbook", json=document_body(), headers=auth)
        other = dev_identity_provider.issue(subject="user-2", tenant_id="tenant-2")

        response = await client.post(
            "/api/v1/answers",
            json={"question": "how do I drain a node"},
            headers={"Authorization": f"Bearer {other}"},
        )

        assert response.json()["grounded"] is False

    async def test_an_empty_question_is_rejected_before_any_work(
        self, client: AsyncClient, backend: Backend, auth: dict[str, str]
    ) -> None:
        response = await client.post("/api/v1/answers", json={"question": ""}, headers=auth)
        assert response.status_code == 422

    async def test_answering_requires_authentication(
        self, client: AsyncClient, backend: Backend
    ) -> None:
        response = await client.post("/api/v1/answers", json={"question": "anything"})
        assert response.status_code == 401
