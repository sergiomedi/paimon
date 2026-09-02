"""Reciprocal Rank Fusion.

Combines the results of several retrievers into one ranking, using positions
rather than scores.

That choice is forced rather than stylistic. BM25 scores have no upper bound and
depend on corpus statistics; cosine similarity is confined to [-1, 1]. Adding or
averaging the two compares quantities with no common scale, and the retriever
whose numbers happen to be larger dominates for no reason connected to relevance.
Normalizing them first only moves the problem: a min-max normalization over a
result set makes the top hit of a list of near-misses score exactly as high as
the top hit of a list of perfect matches.

Rank fusion sidesteps all of it. Only "this retriever ranked the document third"
is used, which is comparable across any pair of retrievers.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from paimon.domain.entities import Chunk
from paimon.domain.ports import SearchHit

DEFAULT_RRF_K = 60
"""The smoothing constant from the original RRF paper.

It damps the influence of the very top positions: without it, rank one would be
worth twice rank two, and a single retriever's confident mistake would outweigh
agreement between all the others. Azure AI Search uses this same value, which
means the fused ordering here and the one Azure produces natively are directly
comparable — the point of ADR-0003's two-backend design.
"""


@dataclass(frozen=True, slots=True)
class Contribution:
    """What one retriever said about one chunk."""

    retriever: str
    rank: int
    score: float


@dataclass(frozen=True, slots=True)
class FusedHit:
    """A chunk, its fused position, and how each retriever found it.

    The contributions are kept rather than collapsed. "Only lexical found this,
    at rank seven" is the single most useful thing to know when a retrieval
    result is surprising, and it is impossible to reconstruct afterwards.
    """

    chunk: Chunk
    score: float
    rank: int
    contributions: tuple[Contribution, ...] = field(default_factory=tuple)

    @property
    def retrievers(self) -> tuple[str, ...]:
        """Which retrievers found this chunk, in contribution order."""
        return tuple(contribution.retriever for contribution in self.contributions)


def reciprocal_rank_fusion(
    result_lists: Sequence[Sequence[SearchHit]],
    *,
    top_k: int,
    rrf_k: int = DEFAULT_RRF_K,
    weights: Mapping[str, float] | None = None,
) -> list[FusedHit]:
    """Fuse several ranked result lists into one.

    Each list contributes ``weight / (rrf_k + rank)`` to every chunk it returned,
    and the sums are ranked.

    Args:
        result_lists: One ranked list per retriever, best first.
        top_k: How many fused hits to return.
        rrf_k: Smoothing constant; smaller values weight top positions more.
        weights: Per-retriever multipliers, defaulting to one. Tuning these is
            an evaluation question, not a matter of taste, which is why they are
            a parameter rather than a constant.

    Returns:
        Fused hits, best first, ranked from one.

    Raises:
        ValueError: If ``rrf_k`` is negative or ``top_k`` is not positive.
    """
    if rrf_k < 0:
        msg = "rrf_k cannot be negative"
        raise ValueError(msg)
    if top_k <= 0:
        msg = "top_k must be positive"
        raise ValueError(msg)

    weights = weights or {}
    scores: dict[str, float] = {}
    chunks: dict[str, Chunk] = {}
    contributions: dict[str, list[Contribution]] = {}

    for results in result_lists:
        for hit in results:
            key = hit.chunk.chunk_id
            weight = weights.get(hit.retriever, 1.0)
            scores[key] = scores.get(key, 0.0) + weight / (rrf_k + hit.rank)
            chunks.setdefault(key, hit.chunk)
            contributions.setdefault(key, []).append(
                Contribution(retriever=hit.retriever, rank=hit.rank, score=hit.score)
            )

    # Ties broken by the best rank any retriever gave the chunk, then by id.
    # Deterministic ordering matters more than the tie-break rule itself: an
    # evaluation run that reorders equal-scoring hits between runs reports noise
    # as a change.
    def sort_key(key: str) -> tuple[float, int, str]:
        best_rank = min(item.rank for item in contributions[key])
        return (-scores[key], best_rank, key)

    ordered = sorted(scores, key=sort_key)

    return [
        FusedHit(
            chunk=chunks[key],
            score=scores[key],
            rank=position,
            contributions=tuple(contributions[key]),
        )
        for position, key in enumerate(ordered[:top_k], start=1)
    ]
