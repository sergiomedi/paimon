"""Tests for the answering use case."""

import pytest

from paimon.application.use_cases import AnswerQuestion, RetrieveChunks
from paimon.domain.entities import Chunk, Document
from paimon.domain.ports import ChunkRecord, IndexDescriptor, SearchFilters
from paimon.infrastructure.tokenization import HeuristicTokenCounter
from tests.fakes import (
    FakeChatModel,
    FakeEmbeddingModel,
    InMemoryDocumentRepository,
    InMemoryVectorStore,
)

TENANT = "tenant-1"
DIMENSIONS = 64


def chunk(chunk_id: str, text: str, ordinal: int = 0) -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        document_id="runbook",
        tenant_id=TENANT,
        ordinal=ordinal,
        text=text,
        start_char=ordinal * 200,
        end_char=ordinal * 200 + len(text),
        token_count=max(len(text.split()), 1),
    )


class Harness:
    """The answering use case wired to reference implementations."""

    def __init__(self, answer: str = "Cordon it first [1].") -> None:
        self.embedding_model = FakeEmbeddingModel(dimensions=DIMENSIONS)
        self.chat_model = FakeChatModel(answer=answer)
        self.repository = InMemoryDocumentRepository()
        self.store = InMemoryVectorStore(
            IndexDescriptor(
                name="test",
                embedding_model_id=self.embedding_model.model_id,
                dimensions=DIMENSIONS,
            )
        )
        self.answer = AnswerQuestion(
            retrieve=RetrieveChunks(self.store, self.embedding_model),
            chat_model=self.chat_model,
            repository=self.repository,
            token_counter=HeuristicTokenCounter(),
        )

    async def index(self, *chunks: Chunk) -> None:
        embeddings = await self.embedding_model.embed_documents([c.text for c in chunks])
        await self.store.upsert(
            [ChunkRecord(chunk=c, embedding=e) for c, e in zip(chunks, embeddings, strict=True)]
        )


@pytest.fixture
def harness() -> Harness:
    return Harness()


class TestGroundedAnswer:
    async def test_it_answers_from_retrieved_material(self, harness: Harness) -> None:
        await harness.index(chunk("c1", "Cordon the node before rebooting it."))
        answer = await harness.answer("how do I drain a node", SearchFilters(tenant_id=TENANT))

        assert answer.grounded
        assert answer.citations[0].chunk_id == "c1"
        assert answer.retrieved >= 1
        assert answer.used_sources >= 1

    async def test_the_model_sees_the_retrieved_sources(self, harness: Harness) -> None:
        await harness.index(chunk("c1", "Cordon the node before rebooting it."))
        await harness.answer("drain", SearchFilters(tenant_id=TENANT))

        (conversation,) = harness.chat_model.calls
        assert "Cordon the node" in conversation[1].content

    async def test_usage_is_reported(self, harness: Harness) -> None:
        """Per-request cost attribution is a Phase 5 deliverable and cannot be
        added after the fact."""
        await harness.index(chunk("c1", "Cordon the node."))
        answer = await harness.answer("drain", SearchFilters(tenant_id=TENANT))

        assert answer.usage is not None
        assert answer.usage.total_tokens > 0


class TestRefusal:
    async def test_nothing_retrieved_means_no_model_call(self, harness: Harness) -> None:
        """The failure mode that makes such systems untrustworthy is a fluent
        answer from parametric memory. The cheapest way to avoid it is not to
        ask."""
        answer = await harness.answer("anything at all", SearchFilters(tenant_id=TENANT))

        assert harness.chat_model.calls == []
        assert answer.grounded is False
        assert answer.citations == ()
        assert "no indexed material" in answer.text

    async def test_another_tenant_gets_the_refusal(self, harness: Harness) -> None:
        await harness.index(chunk("c1", "Cordon the node."))
        answer = await harness.answer("drain", SearchFilters(tenant_id="tenant-2"))

        assert answer.grounded is False
        assert harness.chat_model.calls == []

    async def test_an_uncited_answer_is_reported_as_ungrounded(self) -> None:
        harness = Harness(answer="The node is drained.")
        await harness.index(chunk("c1", "Cordon the node."))
        answer = await harness.answer("drain", SearchFilters(tenant_id=TENANT))

        assert answer.grounded is False
        assert answer.citations == ()


class TestInventedReferences:
    async def test_markers_pointing_nowhere_are_removed_and_counted(self) -> None:
        harness = Harness(answer="Grounded [1]. Invented [9].")
        await harness.index(chunk("c1", "Cordon the node."))
        answer = await harness.answer("drain", SearchFilters(tenant_id=TENANT))

        assert "[9]" not in answer.text
        assert answer.dropped_markers == (9,)
        assert answer.grounded is True


class TestCitationEnrichment:
    async def test_citations_carry_the_document_title_and_uri(self, harness: Harness) -> None:
        await harness.repository.save(
            Document(
                document_id="runbook",
                tenant_id=TENANT,
                source_uri="https://example.test/runbook.md",
                title="Node maintenance",
                text="Cordon the node before rebooting it.",
                content_hash="hash",
                media_type="text/markdown",
            )
        )
        await harness.index(chunk("c1", "Cordon the node before rebooting it."))
        answer = await harness.answer("drain", SearchFilters(tenant_id=TENANT))

        assert answer.citations[0].title == "Node maintenance"
        assert answer.citations[0].source_uri == "https://example.test/runbook.md"
