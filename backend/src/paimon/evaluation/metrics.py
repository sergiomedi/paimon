"""Retrieval metrics."""

import math
from collections.abc import Sequence
from dataclasses import dataclass

from paimon.domain.entities import Chunk
from paimon.evaluation.dataset import EvaluationCase, SupportingPassage


@dataclass(frozen=True, slots=True)
class CaseOutcome:
    """How retrieval did on one question.

    Attributes:
        case_id: The question.
        found: Passages that were retrieved.
        missed: Passages that were not.
        first_relevant_rank: Position of the first useful hit, if any.
        retrieved: How many chunks retrieval returned.
        cutoff: The k these numbers are measured at.
    """

    case_id: str
    found: tuple[SupportingPassage, ...]
    missed: tuple[SupportingPassage, ...]
    first_relevant_rank: int | None
    retrieved: int
    cutoff: int

    @property
    def recall(self) -> float:
        """Fraction of the expected passages that were retrieved."""
        total = len(self.found) + len(self.missed)
        return len(self.found) / total if total else 0.0

    @property
    def precision(self) -> float:
        """Fraction of retrieved chunks that supported something expected.

        Measured against the cutoff rather than the number returned, so a query
        that retrieves three chunks is not scored more leniently than one that
        retrieves ten.
        """
        return len(self.found) / self.cutoff if self.cutoff else 0.0

    @property
    def reciprocal_rank(self) -> float:
        """One over the rank of the first useful hit, or zero if there was none."""
        return 1.0 / self.first_relevant_rank if self.first_relevant_rank else 0.0

    @property
    def is_answerable(self) -> bool:
        """Whether anything useful was retrieved at all.

        The blunt question that matters most: with nothing relevant in context,
        the best a generator can do is refuse.
        """
        return bool(self.found)


@dataclass(frozen=True, slots=True)
class RetrievalMetrics:
    """Aggregate scores over a dataset."""

    cases: int
    cutoff: int
    recall_at_k: float
    precision_at_k: float
    mean_reciprocal_rank: float
    ndcg_at_k: float
    answerable_rate: float


def score_case(case: EvaluationCase, retrieved: Sequence[Chunk], cutoff: int) -> CaseOutcome:
    """Judge one question's retrieval.

    A passage counts as retrieved when a chunk from the right document contains
    its quotation, whitespace-insensitively. Judging by chunk id would make the
    ground truth depend on the chunking policy, which is the variable the
    benchmark exists to change (ADR-0013).

    Args:
        case: The question and its expected passages.
        retrieved: Retrieved chunks, best first.
        cutoff: How many of them to consider.

    Returns:
        What was found, what was missed and where.
    """
    top = list(retrieved[:cutoff])
    found: list[SupportingPassage] = []
    missed: list[SupportingPassage] = []
    first_rank: int | None = None

    for passage in case.supporting:
        rank = next(
            (
                position
                for position, chunk in enumerate(top, start=1)
                if passage.is_supported_by(chunk.document_id, chunk.text)
            ),
            None,
        )
        if rank is None:
            missed.append(passage)
            continue
        found.append(passage)
        first_rank = rank if first_rank is None else min(first_rank, rank)

    return CaseOutcome(
        case_id=case.case_id,
        found=tuple(found),
        missed=tuple(missed),
        first_relevant_rank=first_rank,
        retrieved=len(retrieved),
        cutoff=cutoff,
    )


def _ndcg(outcome: CaseOutcome, relevant_ranks: Sequence[int]) -> float:
    """Normalized discounted cumulative gain for one case, binary relevance."""
    gain = sum(1.0 / math.log2(rank + 1) for rank in relevant_ranks)
    ideal_count = min(len(outcome.found) + len(outcome.missed), outcome.cutoff)
    ideal = sum(1.0 / math.log2(rank + 1) for rank in range(1, ideal_count + 1))
    return gain / ideal if ideal else 0.0


def summarize(
    outcomes: Sequence[CaseOutcome], ranks_per_case: Sequence[Sequence[int]], cutoff: int
) -> RetrievalMetrics:
    """Aggregate case outcomes into dataset-level numbers.

    Macro-averaged: every question counts once, regardless of how many passages
    it expects. A micro average would let one question with eight expected
    passages outweigh eight questions with one, and the dataset would silently
    become a benchmark of that question.

    Args:
        outcomes: One per case.
        ranks_per_case: The ranks at which relevant chunks appeared, per case.
        cutoff: The k the numbers are measured at.

    Returns:
        The aggregate metrics.
    """
    if not outcomes:
        return RetrievalMetrics(
            cases=0,
            cutoff=cutoff,
            recall_at_k=0.0,
            precision_at_k=0.0,
            mean_reciprocal_rank=0.0,
            ndcg_at_k=0.0,
            answerable_rate=0.0,
        )

    count = len(outcomes)
    return RetrievalMetrics(
        cases=count,
        cutoff=cutoff,
        recall_at_k=sum(outcome.recall for outcome in outcomes) / count,
        precision_at_k=sum(outcome.precision for outcome in outcomes) / count,
        mean_reciprocal_rank=sum(outcome.reciprocal_rank for outcome in outcomes) / count,
        ndcg_at_k=sum(
            _ndcg(outcome, ranks) for outcome, ranks in zip(outcomes, ranks_per_case, strict=True)
        )
        / count,
        answerable_rate=sum(1 for outcome in outcomes if outcome.is_answerable) / count,
    )
