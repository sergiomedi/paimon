"""Tracing for the vector stores, by the same decoration as the models.

Same shape as ``models.py`` and, importantly, the same hazard. ``VectorStore``
has a capability protocol beside it — ``NativeHybridSearch``, the store that
fuses dense and lexical retrieval itself — and the application chooses its
retrieval path with an ``isinstance`` check. A wrapper that implemented only
``VectorStore`` would not raise: Azure AI Search would silently stop using its
own fusion and start being fused in-process, which is a change in retrieval
*quality* with nothing anywhere reporting it.

That is the second time this pattern has appeared, and it is why ADR-0026 treats
capability preservation as the thing to test rather than a thing to remember.
"""

from collections.abc import Sequence
from contextlib import AbstractContextManager
from typing import Protocol, runtime_checkable

from opentelemetry.trace import Span

from paimon.domain.ports import (
    ChunkRecord,
    IndexDescriptor,
    NativeHybridSearch,
    SearchFilters,
    SearchHit,
    VectorStore,
)
from paimon.domain.value_objects import Embedding
from paimon.observability.genai import (
    DATA_SOURCE,
    HITS_RETURNED,
    RETRIEVAL_QUERY,
    RETRIEVAL_TOP_K,
    Operation,
    operation_span,
)

#: How a retrieval path is labelled on its span. Which of the two ran is the
#: first thing to know when a query returns something surprising, and it is not
#: otherwise recoverable from a trace.
STRATEGY = "paimon.retrieval.strategy"


@runtime_checkable
class HybridVectorStore(VectorStore, NativeHybridSearch, Protocol):
    """A store that is both, as one type."""


class TracedVectorStore:
    """A vector store that records a span per search."""

    def __init__(self, inner: VectorStore, *, capture_content: bool = False) -> None:
        """Wrap a vector store.

        Args:
            inner: The store doing the work.
            capture_content: Record the query text on the span. Off by default:
                a query is what somebody asked, and the conventions mark it
                opt-in for that reason.
        """
        self._inner = inner
        self._capture_content = capture_content

    @property
    def descriptor(self) -> IndexDescriptor:
        """What this index is and what it will accept."""
        return self._inner.descriptor

    async def upsert(self, records: Sequence[ChunkRecord]) -> None:
        """Write records, untraced.

        Ingestion's cost is the embedding call, which has its own span, and the
        write itself is a database statement that the SQLAlchemy instrumentation
        already records. A span here would add a frame around a frame.
        """
        await self._inner.upsert(records)

    async def delete_document(self, tenant_id: str, document_id: str) -> int:
        """Remove a document's chunks, untraced for the same reason."""
        return await self._inner.delete_document(tenant_id, document_id)

    async def search_dense(
        self, embedding: Embedding, *, top_k: int, filters: SearchFilters
    ) -> list[SearchHit]:
        """Retrieve by vector similarity, recording the search."""
        with self._span("dense", top_k) as span:
            hits = await self._inner.search_dense(embedding, top_k=top_k, filters=filters)
            span.set_attribute(HITS_RETURNED, len(hits))
            return hits

    async def search_lexical(
        self, query: str, *, top_k: int, filters: SearchFilters
    ) -> list[SearchHit]:
        """Retrieve by keyword matching, recording the search."""
        with self._span("lexical", top_k) as span:
            if self._capture_content:
                span.set_attribute(RETRIEVAL_QUERY, query)
            hits = await self._inner.search_lexical(query, top_k=top_k, filters=filters)
            span.set_attribute(HITS_RETURNED, len(hits))
            return hits

    def _span(self, strategy: str, top_k: int) -> AbstractContextManager[Span]:
        """Open a retrieval span for this index."""
        return operation_span(
            Operation.RETRIEVAL,
            self._inner.descriptor.name,
            attributes={
                DATA_SOURCE: self._inner.descriptor.name,
                RETRIEVAL_TOP_K: top_k,
                STRATEGY: strategy,
            },
        )


class TracedHybridVectorStore(TracedVectorStore):
    """The same, for a store that fuses both signals itself."""

    def __init__(self, inner: HybridVectorStore, *, capture_content: bool = False) -> None:
        """Wrap a store with native hybrid search."""
        super().__init__(inner, capture_content=capture_content)
        self._hybrid = inner

    async def search_hybrid(
        self, query: str, embedding: Embedding, *, top_k: int, filters: SearchFilters
    ) -> list[SearchHit]:
        """Retrieve using the store's own fusion, recording the search."""
        with self._span("native_hybrid", top_k) as span:
            if self._capture_content:
                span.set_attribute(RETRIEVAL_QUERY, query)
            hits = await self._hybrid.search_hybrid(query, embedding, top_k=top_k, filters=filters)
            span.set_attribute(HITS_RETURNED, len(hits))
            return hits


def trace_vector_store(inner: VectorStore, *, capture_content: bool = False) -> VectorStore:
    """Wrap a vector store without losing what it can do.

    Args:
        inner: The store to wrap.
        capture_content: Record query text on spans.

    Returns:
        A wrapper that also satisfies :class:`NativeHybridSearch` when the wrapped
        store does. Losing that would not raise — it would silently move Azure AI
        Search off its own fusion and onto in-process fusion, changing retrieval
        quality with nothing reporting it.
    """
    if isinstance(inner, HybridVectorStore):
        return TracedHybridVectorStore(inner, capture_content=capture_content)
    return TracedVectorStore(inner, capture_content=capture_content)


__all__ = [
    "STRATEGY",
    "HybridVectorStore",
    "TracedHybridVectorStore",
    "TracedVectorStore",
    "trace_vector_store",
]
