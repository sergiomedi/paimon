"""Synchronising a whole source.

What is under test is how a *run* behaves, not how a document is ingested — that
has its own tests. The question here is what happens to the other ninety-nine
documents when one of them is broken.
"""

import pytest

from paimon.application.use_cases import IngestDocument, IngestSource
from paimon.domain.errors import SourceUnavailableError
from paimon.domain.ports import IndexDescriptor
from paimon.infrastructure.parsing import MarkdownParser
from paimon.infrastructure.tokenization import HeuristicTokenCounter
from paimon.rag.chunking import Chunker, ChunkingPolicy
from tests.fakes import FakeEmbeddingModel, InMemoryDocumentRepository, InMemoryVectorStore
from tests.fakes.source import InMemoryDocumentSource

TENANT = "tenant-1"
DIMENSIONS = 64

DOCUMENTS = {
    "alpha": b"# Alpha\n\nCordon the node before draining it.\n",
    "beta": b"# Beta\n\nEviction stalls on a disruption budget.\n",
}


@pytest.fixture
def synchronize() -> IngestSource:
    """The use case over a full in-memory pipeline."""
    embedding_model = FakeEmbeddingModel(dimensions=DIMENSIONS)
    return IngestSource(
        IngestDocument(
            parser=MarkdownParser(),
            repository=InMemoryDocumentRepository(),
            store=InMemoryVectorStore(
                IndexDescriptor(
                    name="test",
                    embedding_model_id=embedding_model.model_id,
                    dimensions=DIMENSIONS,
                )
            ),
            embedding_model=embedding_model,
            chunker=Chunker(
                ChunkingPolicy(max_tokens=80, overlap_tokens=10, min_tokens=5),
                HeuristicTokenCounter(),
            ),
        )
    )


class TestSynchronizing:
    async def test_everything_the_source_offers_is_indexed(self, synchronize: IngestSource) -> None:
        result = await synchronize(InMemoryDocumentSource(DOCUMENTS), tenant_id=TENANT)
        assert result.indexed == 2
        assert result.considered == 2
        assert result.failed == ()

    async def test_a_second_pass_costs_nothing(self, synchronize: IngestSource) -> None:
        # The content hash already in ingestion, doing the work a scheduled
        # synchronisation depends on: unchanged documents are not re-embedded.
        source = InMemoryDocumentSource(DOCUMENTS)
        await synchronize(source, tenant_id=TENANT)
        result = await synchronize(source, tenant_id=TENANT)
        assert result.unchanged == 2
        assert result.indexed == 0

    async def test_one_unreadable_document_does_not_end_the_run(
        self, synchronize: IngestSource
    ) -> None:
        source = InMemoryDocumentSource(DOCUMENTS, unreadable=frozenset({"beta"}))
        result = await synchronize(source, tenant_id=TENANT)
        assert result.indexed == 1
        assert [document_id for document_id, _ in result.failed] == ["beta"]

    async def test_a_failure_says_which_document_and_why(self, synchronize: IngestSource) -> None:
        # Counted failures are useless: a caller who cannot see which document
        # failed has to diff two corpora to find out.
        source = InMemoryDocumentSource(DOCUMENTS, unreadable=frozenset({"beta"}))
        result = await synchronize(source, tenant_id=TENANT)
        document_id, reason = result.failed[0]
        assert document_id == "beta"
        assert "could not be read" in reason

    async def test_an_unreachable_source_is_raised_not_reported(
        self, synchronize: IngestSource
    ) -> None:
        # There is nothing to be partial about. A run that reports "0 indexed,
        # 0 failed" for an outage is a run that lies.
        source = InMemoryDocumentSource(DOCUMENTS, unreachable=True)
        with pytest.raises(SourceUnavailableError):
            await synchronize(source, tenant_id=TENANT)

    async def test_the_tenant_comes_from_the_caller(self, synchronize: IngestSource) -> None:
        # Never from the source. Whose corpus a document joins is a fact about
        # the authenticated caller, not something an external system gets a say
        # in — and a source that could choose would be a tenancy bypass.
        result = await synchronize(InMemoryDocumentSource(DOCUMENTS), tenant_id="tenant-2")
        assert result.indexed == 2
        result = await synchronize(InMemoryDocumentSource(DOCUMENTS), tenant_id=TENANT)
        assert result.indexed == 2

    async def test_the_result_names_the_source(self, synchronize: IngestSource) -> None:
        source = InMemoryDocumentSource(DOCUMENTS, name="handbook")
        assert (await synchronize(source, tenant_id=TENANT)).source == "handbook"
