"""Tests for the ingestion use case."""

import pytest

from paimon.application.use_cases import IngestDocument, SourceDocument
from paimon.domain.errors import IngestionError, UnsupportedMediaTypeError
from paimon.domain.ports import IndexDescriptor, SearchFilters
from paimon.infrastructure.parsing import MarkdownParser
from paimon.infrastructure.tokenization import HeuristicTokenCounter
from paimon.rag.chunking import Chunker, ChunkingPolicy
from tests.fakes import FakeEmbeddingModel, InMemoryDocumentRepository, InMemoryVectorStore

TENANT = "tenant-1"
RUNBOOK = b"""# Node maintenance

Nodes are drained before any kernel upgrade, without exception.

## Draining

Cordon the node first so the scheduler stops placing new pods on it.
Then evict the running pods and wait for them to reschedule elsewhere.
"""


class Harness:
    """The use case wired to reference implementations."""

    def __init__(self) -> None:
        self.embedding_model = FakeEmbeddingModel(dimensions=64)
        self.repository = InMemoryDocumentRepository()
        self.store = InMemoryVectorStore(
            IndexDescriptor(
                name="test",
                embedding_model_id=self.embedding_model.model_id,
                dimensions=self.embedding_model.dimensions,
            )
        )
        self.ingest = IngestDocument(
            parser=MarkdownParser(),
            repository=self.repository,
            store=self.store,
            embedding_model=self.embedding_model,
            chunker=Chunker(
                ChunkingPolicy(max_tokens=60, overlap_tokens=10, min_tokens=5),
                HeuristicTokenCounter(),
            ),
        )


@pytest.fixture
def harness() -> Harness:
    return Harness()


def source(raw: bytes = RUNBOOK, **overrides: object) -> SourceDocument:
    fields: dict[str, object] = {
        "tenant_id": TENANT,
        "document_id": "doc-1",
        "source_uri": "https://example.test/runbook.md",
        "raw": raw,
        "media_type": "text/markdown",
    }
    fields.update(overrides)
    return SourceDocument(**fields)  # type: ignore[arg-type]


class TestFirstIngestion:
    async def test_it_indexes_the_document(self, harness: Harness) -> None:
        result = await harness.ingest(source())

        assert result.unchanged is False
        assert result.chunks_indexed > 0

        hits = await harness.store.search_lexical(
            "cordon", top_k=10, filters=SearchFilters(tenant_id=TENANT)
        )
        assert hits

    async def test_it_stores_the_document_for_citation_resolution(self, harness: Harness) -> None:
        """The chunks alone cannot show a claim in the context it was made in."""
        await harness.ingest(source())
        stored = await harness.repository.get(TENANT, "doc-1")

        assert stored is not None
        assert "Cordon the node" in stored.text

    async def test_offsets_resolve_against_the_stored_document(self, harness: Harness) -> None:
        """The whole point of the offsets, asserted end to end."""
        await harness.ingest(source())
        stored = await harness.repository.get(TENANT, "doc-1")
        assert stored is not None

        hits = await harness.store.search_lexical(
            "cordon", top_k=1, filters=SearchFilters(tenant_id=TENANT)
        )
        chunk = hits[0].chunk
        assert stored.text[chunk.start_char : chunk.end_char] == chunk.text

    async def test_the_title_comes_from_the_document(self, harness: Harness) -> None:
        await harness.ingest(source())
        stored = await harness.repository.get(TENANT, "doc-1")

        assert stored is not None
        assert stored.title == "Node maintenance"

    async def test_the_source_uri_is_the_fallback_title(self, harness: Harness) -> None:
        await harness.ingest(source(b"Prose with no heading at all, but long enough to keep."))
        stored = await harness.repository.get(TENANT, "doc-1")

        assert stored is not None
        assert stored.title == "https://example.test/runbook.md"

    async def test_what_is_embedded_carries_the_heading_context(self, harness: Harness) -> None:
        """The index is built on the contextualized text, not on the bare slice."""
        await harness.ingest(source())
        (batch,) = harness.embedding_model.document_batches

        assert any(text.startswith("Node maintenance > Draining") for text in batch)

    async def test_supplied_metadata_is_kept(self, harness: Harness) -> None:
        await harness.ingest(source(metadata={"team": "platform"}))
        stored = await harness.repository.get(TENANT, "doc-1")

        assert stored is not None
        assert stored.metadata["team"] == "platform"


class TestIdempotence:
    async def test_re_ingesting_unchanged_bytes_does_no_work(self, harness: Harness) -> None:
        """A corpus is re-ingested on a schedule and embeddings are the expensive
        part, so an unchanged document must cost a hash comparison and nothing
        more."""
        await harness.ingest(source())
        batches_after_first = len(harness.embedding_model.document_batches)

        second = await harness.ingest(source())

        assert second.unchanged is True
        assert second.chunks_indexed == 0
        assert len(harness.embedding_model.document_batches) == batches_after_first

    async def test_a_changed_document_is_reindexed(self, harness: Harness) -> None:
        await harness.ingest(source())
        revised = RUNBOOK.replace(b"without exception", b"unless the vendor says otherwise")

        result = await harness.ingest(source(revised))

        assert result.unchanged is False
        hits = await harness.store.search_lexical(
            "vendor", top_k=5, filters=SearchFilters(tenant_id=TENANT)
        )
        assert hits

    async def test_a_shorter_revision_removes_the_text_it_dropped(self, harness: Harness) -> None:
        """Merging instead of replacing would leave the platform answering from
        text the author deleted."""
        await harness.ingest(source())
        await harness.ingest(source(b"# Node maintenance\n\nThis procedure has been retired.\n"))

        hits = await harness.store.search_lexical(
            "cordon", top_k=10, filters=SearchFilters(tenant_id=TENANT)
        )
        assert hits == []


class TestFailures:
    async def test_an_unsupported_type_is_refused(self, harness: Harness) -> None:
        with pytest.raises(UnsupportedMediaTypeError):
            await harness.ingest(source(b"%PDF-1.7", media_type="application/pdf"))

    async def test_a_document_that_chunks_to_nothing_is_refused(self, harness: Harness) -> None:
        with pytest.raises(IngestionError, match="produced no chunks"):
            await harness.ingest(source(b"# Heading only\n"))

    async def test_a_failed_ingestion_leaves_no_document_recorded(self, harness: Harness) -> None:
        """The stored hash marks a document as ingested; recording it before the
        index is written would make a failed run look complete and never retry."""
        with pytest.raises(IngestionError):
            await harness.ingest(source(b"# Heading only\n"))

        assert await harness.repository.get(TENANT, "doc-1") is None


class TestTenancy:
    async def test_documents_are_isolated_by_tenant(self, harness: Harness) -> None:
        await harness.ingest(source())

        assert await harness.repository.get("tenant-2", "doc-1") is None
        assert (
            await harness.store.search_lexical(
                "cordon", top_k=10, filters=SearchFilters(tenant_id="tenant-2")
            )
            == []
        )
