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
    ManagedIndex,
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


@runtime_checkable
class ManagedVectorStore(VectorStore, ManagedIndex, Protocol):
    """A store whose index has to be created, as one type."""


class _DelegatesIndexManagement:
    """Passes index creation through to the wrapped store.

    A mixin rather than three copies of one method, because the number of wrapper
    classes is the product of the capabilities and this is the second of them.
    Nothing here is traced: creating an index is administrative, happens once, by
    hand, and is not part of any request — a span for it would be a span nobody
    ever looks at, on a code path that runs before there is anything to look at
    it with.
    """

    _managed: ManagedIndex

    async def ensure_index(self) -> None:
        """Create the index, or bring an existing one up to this definition."""
        await self._managed.ensure_index()


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


class TracedManagedVectorStore(_DelegatesIndexManagement, TracedVectorStore):
    """Traced, and still able to have its index created."""

    def __init__(self, inner: ManagedVectorStore, *, capture_content: bool = False) -> None:
        """Wrap a store whose index is managed."""
        super().__init__(inner, capture_content=capture_content)
        self._managed = inner


class TracedHybridManagedVectorStore(_DelegatesIndexManagement, TracedHybridVectorStore):
    """Both capabilities, which is what Azure AI Search actually is."""

    def __init__(self, inner: VectorStore, *, capture_content: bool = False) -> None:
        """Wrap a store that fuses natively *and* manages its index."""
        assert isinstance(inner, HybridVectorStore)  # noqa: S101  chosen by the factory
        assert isinstance(inner, ManagedIndex)  # noqa: S101  chosen by the factory
        super().__init__(inner, capture_content=capture_content)
        self._managed = inner


def trace_vector_store(inner: VectorStore, *, capture_content: bool = False) -> VectorStore:
    """Wrap a vector store without losing what it can do.

    Args:
        inner: The store to wrap.
        capture_content: Record query text on spans.

    Returns:
        A wrapper satisfying every capability protocol the wrapped store does.

    Two capabilities means four wrappers, and that product is the cost of
    expressing capabilities as types rather than as flags. It is a cost worth
    paying only because losing one is silent: dropping ``NativeHybridSearch``
    moves Azure AI Search off its own fusion and onto in-process fusion — a
    change in retrieval *quality* that raises nothing — and dropping
    ``ManagedIndex`` hides the command that creates the index behind a message
    saying this store has none.

    The second of those is not hypothetical. ``ManagedIndex`` was added and this
    factory was not, so the benchmark that exists to exercise Azure AI Search
    reported that Azure AI Search had no index to create. The test that now
    enumerates the capability protocols is what makes a third one safe.
    """
    managed = isinstance(inner, ManagedIndex)
    if isinstance(inner, HybridVectorStore):
        if managed:
            return TracedHybridManagedVectorStore(inner, capture_content=capture_content)
        return TracedHybridVectorStore(inner, capture_content=capture_content)
    if managed:
        assert isinstance(inner, ManagedVectorStore)  # noqa: S101  narrowed by the check above
        return TracedManagedVectorStore(inner, capture_content=capture_content)
    return TracedVectorStore(inner, capture_content=capture_content)


__all__ = [
    "STRATEGY",
    "HybridVectorStore",
    "ManagedVectorStore",
    "TracedHybridManagedVectorStore",
    "TracedHybridVectorStore",
    "TracedManagedVectorStore",
    "TracedVectorStore",
    "trace_vector_store",
]
