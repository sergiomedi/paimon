"""Ports for storing and retrieving chunks."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from paimon.domain.entities import Chunk
from paimon.domain.value_objects import Embedding


@dataclass(frozen=True, slots=True)
class IndexDescriptor:
    """What an index is, and what it will accept.

    An index is bound to one embedding model and one dimensionality for its whole
    life. Changing either means building a new index and reindexing, so the
    binding is stated rather than assumed, and writes that disagree with it are
    refused.
    """

    name: str
    embedding_model_id: str
    dimensions: int


@dataclass(frozen=True, slots=True)
class ChunkRecord:
    """A chunk together with the embedding to index it by."""

    chunk: Chunk
    embedding: Embedding


@dataclass(frozen=True, slots=True)
class SearchHit:
    """One result from one retriever.

    ``rank`` is carried alongside ``score`` because rank-based fusion needs
    positions, not scores: BM25 is unbounded while cosine similarity is not, and
    combining the two numerically compares quantities that have no common scale.

    Attributes:
        chunk: The retrieved chunk.
        score: The retriever's own score, on its own scale.
        rank: Position in this retriever's result list, starting at one.
        retriever: Which retriever produced the hit, kept for fusion and for
            debugging why something did or did not surface.
    """

    chunk: Chunk
    score: float
    rank: int
    retriever: str

    def __post_init__(self) -> None:
        """Reject a hit that cannot participate in rank fusion.

        Raises:
            ValueError: If the rank is not a positive position.
        """
        if self.rank < 1:
            msg = "ranks are one-based positions"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class SearchFilters:
    """Restrictions applied before ranking.

    Tenant is separate from the free-form filters and always required: it is the
    isolation boundary, and an optional isolation boundary is not one.
    """

    tenant_id: str
    document_ids: frozenset[str] | None = None
    metadata: Mapping[str, str] = field(default_factory=dict)


@runtime_checkable
class VectorStore(Protocol):
    """Stores chunks and retrieves them by meaning and by wording.

    Both retrieval methods are on the port because hybrid retrieval needs both,
    and a store that can only do one of them cannot serve this platform. Native
    hybrid retrieval is a separate, optional capability — see
    :class:`NativeHybridSearch`.
    """

    @property
    def descriptor(self) -> IndexDescriptor:
        """The index this store writes to and reads from."""
        ...

    async def upsert(self, records: Sequence[ChunkRecord]) -> None:
        """Insert or replace chunks, keyed by chunk id.

        Idempotent by chunk id, so re-ingesting an unchanged document is safe and
        a partially failed ingestion can simply be repeated.

        Args:
            records: Chunks with their embeddings.

        Raises:
            IndexMismatchError: If any embedding was produced by a different model
                or has a different dimensionality than the index declares.
        """
        ...

    async def delete_document(self, tenant_id: str, document_id: str) -> int:
        """Remove every chunk of a document.

        Args:
            tenant_id: Owning organization.
            document_id: Document whose chunks are removed.

        Returns:
            How many chunks were removed.
        """
        ...

    async def search_dense(
        self, embedding: Embedding, *, top_k: int, filters: SearchFilters
    ) -> list[SearchHit]:
        """Retrieve by vector similarity.

        Args:
            embedding: The query embedding.
            top_k: Maximum hits to return.
            filters: Tenant and any further restrictions.

        Returns:
            Hits ordered best first, ranked from one.

        Raises:
            IndexMismatchError: If the query embedding does not match the index.
        """
        ...

    async def search_lexical(
        self, query: str, *, top_k: int, filters: SearchFilters
    ) -> list[SearchHit]:
        """Retrieve by keyword matching.

        Args:
            query: The raw query text.
            top_k: Maximum hits to return.
            filters: Tenant and any further restrictions.

        Returns:
            Hits ordered best first, ranked from one.
        """
        ...


@runtime_checkable
class NativeHybridSearch(Protocol):
    """A store that fuses dense and lexical retrieval itself.

    This is the capability flag of ADR-0003 expressed as a type rather than a
    boolean: a store either satisfies this protocol or it does not, and the
    application chooses its path with a check the type checker understands.
    Backends without it are served by fusing in the application layer — the
    feature is exposed explicitly instead of being silently degraded.
    """

    async def search_hybrid(
        self, query: str, embedding: Embedding, *, top_k: int, filters: SearchFilters
    ) -> list[SearchHit]:
        """Retrieve using the store's own fusion of both signals.

        Args:
            query: The raw query text.
            embedding: The query embedding.
            top_k: Maximum hits to return.
            filters: Tenant and any further restrictions.

        Returns:
            Fused hits ordered best first, ranked from one.
        """
        ...
