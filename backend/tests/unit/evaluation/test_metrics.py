"""Tests for the retrieval metrics."""

import pytest

from paimon.domain.entities import Chunk
from paimon.evaluation import EvaluationCase, SupportingPassage, score_case, summarize
from paimon.evaluation.metrics import CaseOutcome
from paimon.evaluation.runner import _relevant_ranks


def chunk(document_id: str, text: str, ordinal: int = 0) -> Chunk:
    return Chunk(
        chunk_id=f"{document_id}:{ordinal}",
        document_id=document_id,
        tenant_id="t",
        ordinal=ordinal,
        text=text,
        start_char=ordinal * 100,
        end_char=ordinal * 100 + len(text),
        token_count=max(len(text.split()), 1),
    )


def case(*quotes: tuple[str, str]) -> EvaluationCase:
    return EvaluationCase(
        case_id="q1",
        question="how do I drain a node?",
        supporting=tuple(
            SupportingPassage(document_id=document_id, quote=quote) for document_id, quote in quotes
        ),
    )


class TestScoring:
    def test_a_retrieved_passage_is_found(self) -> None:
        outcome = score_case(
            case(("runbook", "cordon the node")),
            [chunk("runbook", "First, cordon the node and wait.")],
            cutoff=8,
        )
        assert outcome.is_answerable
        assert outcome.recall == 1.0
        assert outcome.first_relevant_rank == 1

    def test_a_passage_below_the_cutoff_is_missed(self) -> None:
        """Retrieving the right passage at rank twenty is not retrieving it: the
        prompt only ever sees the top few."""
        chunks = [chunk("other", f"noise {index}", index) for index in range(5)]
        chunks.append(chunk("runbook", "cordon the node", 5))

        outcome = score_case(case(("runbook", "cordon the node")), chunks, cutoff=3)

        assert not outcome.is_answerable
        assert outcome.recall == 0.0

    def test_partial_recall_is_reported(self) -> None:
        outcome = score_case(
            case(("runbook", "cordon the node"), ("runbook", "evict the pods")),
            [chunk("runbook", "First, cordon the node.")],
            cutoff=8,
        )
        assert outcome.recall == 0.5
        assert len(outcome.missed) == 1

    def test_precision_is_measured_against_the_cutoff(self) -> None:
        """Otherwise a query that returns three chunks is scored more leniently
        than one that returns ten."""
        outcome = score_case(
            case(("runbook", "cordon the node")),
            [chunk("runbook", "cordon the node")],
            cutoff=8,
        )
        assert outcome.precision == pytest.approx(1 / 8)

    def test_the_first_relevant_rank_is_the_earliest_one(self) -> None:
        chunks = [
            chunk("noise", "unrelated", 0),
            chunk("runbook", "evict the pods", 1),
            chunk("runbook", "cordon the node", 2),
        ]
        outcome = score_case(
            case(("runbook", "cordon the node"), ("runbook", "evict the pods")),
            chunks,
            cutoff=8,
        )
        assert outcome.first_relevant_rank == 2
        assert outcome.reciprocal_rank == pytest.approx(0.5)

    def test_nothing_retrieved_scores_zero_rather_than_failing(self) -> None:
        outcome = score_case(case(("runbook", "cordon")), [], cutoff=8)

        assert outcome.recall == 0.0
        assert outcome.reciprocal_rank == 0.0
        assert not outcome.is_answerable


class TestAggregation:
    def _outcome(self, recall_found: int, total: int, rank: int | None) -> CaseOutcome:
        passages = tuple(
            SupportingPassage(document_id="d", quote=f"q{index}") for index in range(total)
        )
        return CaseOutcome(
            case_id="c",
            found=passages[:recall_found],
            missed=passages[recall_found:],
            first_relevant_rank=rank,
            retrieved=8,
            cutoff=8,
        )

    def test_every_question_counts_once(self) -> None:
        """Macro average. A micro average would let one question expecting eight
        passages outweigh eight questions expecting one, and the dataset would
        quietly become a benchmark of that question."""
        outcomes = [self._outcome(1, 1, 1), self._outcome(0, 8, None)]
        metrics = summarize(outcomes, [[1], []], cutoff=8)

        assert metrics.recall_at_k == pytest.approx(0.5)
        assert metrics.answerable_rate == pytest.approx(0.5)

    def test_an_empty_run_reports_zeros_rather_than_dividing_by_zero(self) -> None:
        metrics = summarize([], [], cutoff=8)
        assert metrics.cases == 0
        assert metrics.recall_at_k == 0.0

    def test_ndcg_rewards_a_higher_rank(self) -> None:
        high = summarize([self._outcome(1, 1, 1)], [[1]], cutoff=8)
        low = summarize([self._outcome(1, 1, 5)], [[5]], cutoff=8)

        assert high.ndcg_at_k > low.ndcg_at_k
        assert high.ndcg_at_k == pytest.approx(1.0)


class TestRankExtraction:
    def test_relevant_ranks_are_one_based_positions(self) -> None:
        chunks = [chunk("noise", "x", 0), chunk("runbook", "cordon the node", 1)]
        ranks = _relevant_ranks(case(("runbook", "cordon the node")), chunks, cutoff=8)
        assert ranks == [2]
