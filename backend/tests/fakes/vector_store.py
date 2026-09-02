"""An in-memory VectorStore, used as the reference implementation."""

import re
from collections.abc import Sequence

from paimon.domain.errors import IndexMismatchError
from paimon.domain.ports import ChunkRecord, IndexDescriptor, SearchFilters, SearchHit
from paimon.domain.value_objects import Embedding, cosine_similarity

_TOKEN = re.compile(r"[a-z0-9]+")


class InMemoryVectorStore:
    """Reference implementation of the VectorStore port.

    Exists to keep the contract suite honest: a contract nothing satisfies is a
    wish list. It is also what the use-case tests retrieve from, so those tests
    need neither a database nor a network.
    """

    def __init__(self, descriptor: IndexDescriptor) -> None:
        self._descriptor = descriptor
        self._records: dict[str, ChunkRecord] = {}
        # Recorded so tests can assert which retrievers ran, not merely what came
        # back: choosing a strategy is a behaviour worth pinning.
        self.dense_calls: list[int] = []
        self.lexical_calls: list[tuple[str, int]] = []

    @property
    def descriptor(self) -> IndexDescriptor:
        return self._descriptor

    def _reject_mismatch(self, embedding: Embedding) -> None:
        if embedding.model_id != self._descriptor.embedding_model_id:
            msg = (
                f"index '{self._descriptor.name}' holds embeddings from "
                f"'{self._descriptor.embedding_model_id}', got '{embedding.model_id}'"
            )
            raise IndexMismatchError(msg)
        if embedding.dimensions != self._descriptor.dimensions:
            msg = (
                f"index '{self._descriptor.name}' has {self._descriptor.dimensions} "
                f"dimensions, got {embedding.dimensions}"
            )
            raise IndexMismatchError(msg)

    async def upsert(self, records: Sequence[ChunkRecord]) -> None:
        for record in records:
            self._reject_mismatch(record.embedding)
        # Validated in full before anything is written, so a rejected batch leaves
        # the index exactly as it was.
        for record in records:
            self._records[record.chunk.chunk_id] = record

    async def delete_document(self, tenant_id: str, document_id: str) -> int:
        doomed = [
            chunk_id
            for chunk_id, record in self._records.items()
            if record.chunk.tenant_id == tenant_id and record.chunk.document_id == document_id
        ]
        for chunk_id in doomed:
            del self._records[chunk_id]
        return len(doomed)

    def _visible(self, filters: SearchFilters) -> list[ChunkRecord]:
        return [
            record
            for record in self._records.values()
            if record.chunk.tenant_id == filters.tenant_id
            and (filters.document_ids is None or record.chunk.document_id in filters.document_ids)
        ]

    async def search_dense(
        self, embedding: Embedding, *, top_k: int, filters: SearchFilters
    ) -> list[SearchHit]:
        self._reject_mismatch(embedding)
        self.dense_calls.append(top_k)
        scored = [
            (cosine_similarity(embedding, record.embedding), record)
            for record in self._visible(filters)
        ]
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [
            SearchHit(chunk=record.chunk, score=score, rank=position, retriever="dense")
            for position, (score, record) in enumerate(scored[:top_k], start=1)
        ]

    async def search_lexical(
        self, query: str, *, top_k: int, filters: SearchFilters
    ) -> list[SearchHit]:
        self.lexical_calls.append((query, top_k))
        terms = set(_TOKEN.findall(query.lower()))
        scored = []
        for record in self._visible(filters):
            tokens = _TOKEN.findall(record.chunk.text.lower())
            overlap = sum(1 for token in tokens if token in terms)
            if overlap:
                scored.append((overlap / len(tokens), record))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [
            SearchHit(chunk=record.chunk, score=score, rank=position, retriever="lexical")
            for position, (score, record) in enumerate(scored[:top_k], start=1)
        ]


class InMemoryHybridVectorStore(InMemoryVectorStore):
    """An in-memory store that also fuses natively.

    Stands in for Azure AI Search, whose own fusion the application must defer to
    rather than duplicate. The fusion here is intentionally trivial — the point of
    the fake is the code path, not the algorithm.
    """

    def __init__(self, descriptor: IndexDescriptor) -> None:
        super().__init__(descriptor)
        self.hybrid_calls: list[tuple[str, int]] = []

    async def search_hybrid(
        self, query: str, embedding: Embedding, *, top_k: int, filters: SearchFilters
    ) -> list[SearchHit]:
        self.hybrid_calls.append((query, top_k))
        self._reject_mismatch(embedding)
        dense = await self.search_dense(embedding, top_k=top_k, filters=filters)
        return [
            SearchHit(chunk=hit.chunk, score=hit.score, rank=position, retriever="hybrid")
            for position, hit in enumerate(dense, start=1)
        ]
