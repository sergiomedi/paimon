"""Retrieval metrics."""

import math
from collections.abc import Sequence
from dataclasses import dataclass

from paimon.domain.entities import Chunk
from paimon.evaluation.dataset import EvaluationCase, SupportingPassage
from paimon.evaluation.statistics import Estimate, clustered_estimate, estimate


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
    """Aggregate scores over a dataset, each with its uncertainty.

    Every number is an :class:`~paimon.evaluation.statistics.Estimate` rather
    than a float, and that is the point of the type. Fifteen questions produce
    averages that move by several points on nothing at all; a bare mean invites a
    reader to compare two of them and conclude something, which is the mistake
    this dataset is small enough to make constantly.

    The standard errors are **clustered by document**. Questions about one runbook
    share its wording and whatever the chunker did to it, so counting them as
    independent observations overstates confidence — by a factor of three or more
    in the literature.
    """

    cases: int
    cutoff: int
    recall_at_k: Estimate
    precision_at_k: Estimate
    mean_reciprocal_rank: Estimate
    ndcg_at_k: Estimate
    answerable_rate: Estimate


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


def per_case_scores(
    outcomes: Sequence[CaseOutcome], ranks_per_case: Sequence[Sequence[int]]
) -> dict[str, list[float]]:
    """One score per case per metric, which is what a paired comparison needs.

    Aggregates cannot be compared question by question, and question by question
    is where the variance goes (ADR-0029). So the per-case vectors are produced
    once, used for the aggregates, and kept.
    """
    return {
        "recall_at_k": [outcome.recall for outcome in outcomes],
        "precision_at_k": [outcome.precision for outcome in outcomes],
        "mean_reciprocal_rank": [outcome.reciprocal_rank for outcome in outcomes],
        "ndcg_at_k": [
            _ndcg(outcome, ranks) for outcome, ranks in zip(outcomes, ranks_per_case, strict=True)
        ],
        "answerable_rate": [1.0 if outcome.is_answerable else 0.0 for outcome in outcomes],
    }


def summarize(
    outcomes: Sequence[CaseOutcome],
    ranks_per_case: Sequence[Sequence[int]],
    cutoff: int,
    clusters: Sequence[str] | None = None,
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
        clusters: Which group each case belongs to, when the cases are
            correlated. None treats every case as independent, which is only
            true of a dataset whose questions come from different documents.

    Returns:
        The aggregate metrics.
    """
    empty = Estimate(mean=0.0, standard_error=0.0, n=0)
    if not outcomes:
        return RetrievalMetrics(
            cases=0,
            cutoff=cutoff,
            recall_at_k=empty,
            precision_at_k=empty,
            mean_reciprocal_rank=empty,
            ndcg_at_k=empty,
            answerable_rate=empty,
        )

    scores = per_case_scores(outcomes, ranks_per_case)

    def measured(name: str) -> Estimate:
        values = scores[name]
        return clustered_estimate(values, clusters) if clusters is not None else estimate(values)

    return RetrievalMetrics(
        cases=len(outcomes),
        cutoff=cutoff,
        recall_at_k=measured("recall_at_k"),
        precision_at_k=measured("precision_at_k"),
        mean_reciprocal_rank=measured("mean_reciprocal_rank"),
        ndcg_at_k=measured("ndcg_at_k"),
        answerable_rate=measured("answerable_rate"),
    )
