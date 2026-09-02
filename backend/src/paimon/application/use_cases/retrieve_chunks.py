"""Retrieving the chunks that bear on a question."""

from dataclasses import dataclass, field
from typing import Literal

from paimon.domain.ports import (
    EmbeddingModel,
    NativeHybridSearch,
    SearchFilters,
    SearchHit,
    VectorStore,
)
from paimon.rag.fusion import DEFAULT_RRF_K, Contribution, FusedHit, reciprocal_rank_fusion

Strategy = Literal["native_hybrid", "fused"]


@dataclass(frozen=True, slots=True)
class RetrievalPolicy:
    """How much to retrieve, and how to combine it.

    Attributes:
        top_k: Hits returned to the caller.
        candidates_per_retriever: Hits taken from each retriever before fusion.
            Deliberately larger than ``top_k``: a chunk ranked eighth by one
            retriever and unseen by the other can still finish first once fused,
            and fusing only the top few would have thrown it away before the
            fusion had a chance to find it.
        rrf_k: Smoothing constant for the fusion.
        weights: Per-retriever multipliers. An evaluation question, not a
            matter of taste.
    """

    top_k: int = 8
    candidates_per_retriever: int = 40
    rrf_k: int = DEFAULT_RRF_K
    weights: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Reject a policy that cannot return what it promises.

        Raises:
            ValueError: If either limit is not positive, or fewer candidates are
                gathered than the caller asked to receive.
        """
        if self.top_k <= 0:
            msg = "top_k must be positive"
            raise ValueError(msg)
        if self.candidates_per_retriever < self.top_k:
            msg = "candidates_per_retriever cannot be smaller than top_k"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    """What retrieval found, and how.

    ``strategy`` is reported rather than hidden: the two backends of ADR-0003
    reach an answer by different routes, and a benchmark that cannot tell which
    route produced a number is not measuring anything in particular.
    """

    hits: tuple[FusedHit, ...]
    strategy: Strategy


class RetrieveChunks:
    """Finds the chunks relevant to a question, by meaning and by wording.

    Dense retrieval alone misses exact tokens — an error code, a flag, a hostname
    — because an embedding of a rare string carries little signal. Lexical
    retrieval alone misses paraphrase, which is most of how people ask questions.
    Running both and fusing the rankings is what makes a query like "why did
    eviction hang" reach a runbook that says "pods stall without a disruption
    budget".

    When the store fuses natively — Azure AI Search does — that path is used
    instead, because its fusion has access to information this layer does not.
    """

    def __init__(
        self,
        store: VectorStore,
        embedding_model: EmbeddingModel,
        policy: RetrievalPolicy | None = None,
    ) -> None:
        """Initialise the use case.

        Args:
            store: Where chunks are retrieved from.
            embedding_model: Used to embed the query.
            policy: Retrieval limits and fusion settings.
        """
        self._store = store
        self._embedding_model = embedding_model
        self._policy = policy or RetrievalPolicy()

    async def __call__(self, query: str, filters: SearchFilters) -> RetrievalResult:
        """Retrieve the chunks that bear on a query.

        Args:
            query: The question, as asked.
            filters: Tenant and any further restrictions.

        Returns:
            Fused hits, best first, and which strategy produced them.
        """
        if not query.strip():
            return RetrievalResult(hits=(), strategy="fused")

        embedding = await self._embedding_model.embed_query(query)

        if isinstance(self._store, NativeHybridSearch):
            hits = await self._store.search_hybrid(
                query, embedding, top_k=self._policy.top_k, filters=filters
            )
            return RetrievalResult(hits=self._adopt(hits), strategy="native_hybrid")

        dense = await self._store.search_dense(
            embedding, top_k=self._policy.candidates_per_retriever, filters=filters
        )
        lexical = await self._store.search_lexical(
            query, top_k=self._policy.candidates_per_retriever, filters=filters
        )
        fused = reciprocal_rank_fusion(
            [dense, lexical],
            top_k=self._policy.top_k,
            rrf_k=self._policy.rrf_k,
            weights=self._policy.weights,
        )
        return RetrievalResult(hits=tuple(fused), strategy="fused")

    @staticmethod
    def _adopt(hits: list[SearchHit]) -> tuple[FusedHit, ...]:
        """Present natively fused hits in the same shape as locally fused ones.

        The caller should not have to branch on which backend answered.
        """
        return tuple(
            FusedHit(
                chunk=hit.chunk,
                score=hit.score,
                rank=hit.rank,
                contributions=(
                    Contribution(retriever=hit.retriever, rank=hit.rank, score=hit.score),
                ),
            )
            for hit in hits
        )
