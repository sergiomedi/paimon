"""Tests for the benchmark runner."""

from pathlib import Path

import pytest

from paimon.domain.entities import Chunk
from paimon.domain.ports import SearchFilters
from paimon.evaluation import (
    EvaluationCase,
    EvaluationDataset,
    SupportingPassage,
    run_benchmark,
)


def chunk(document_id: str, text: str, ordinal: int = 0) -> Chunk:
    return Chunk(
        chunk_id=f"{document_id}:{ordinal}",
        document_id=document_id,
        tenant_id="benchmark",
        ordinal=ordinal,
        text=text,
        start_char=ordinal * 100,
        end_char=ordinal * 100 + len(text),
        token_count=max(len(text.split()), 1),
    )


class ScriptedRetriever:
    """Returns a fixed answer per question and records what it was asked."""

    def __init__(self, answers: dict[str, list[Chunk]]) -> None:
        self._answers = answers
        self.questions: list[str] = []
        self.tenants: list[str] = []

    async def retrieve(self, question: str, filters: SearchFilters) -> list[Chunk]:
        self.questions.append(question)
        self.tenants.append(filters.tenant_id)
        return self._answers.get(question, [])


def dataset() -> EvaluationDataset:
    return EvaluationDataset(
        name="test-set",
        cases=(
            EvaluationCase(
                case_id="q1",
                question="how do I drain a node?",
                supporting=(SupportingPassage(document_id="runbook", quote="cordon the node"),),
            ),
            EvaluationCase(
                case_id="q2",
                question="what caused INC-2451?",
                supporting=(SupportingPassage(document_id="postmortem", quote="pool exhaustion"),),
            ),
        ),
    )


class TestRunning:
    async def test_it_asks_every_question_as_the_given_tenant(self) -> None:
        retriever = ScriptedRetriever({})
        await run_benchmark(dataset(), retriever, tenant_id="benchmark")

        assert retriever.questions == ["how do I drain a node?", "what caused INC-2451?"]
        assert set(retriever.tenants) == {"benchmark"}

    async def test_it_scores_each_case(self) -> None:
        retriever = ScriptedRetriever(
            {
                "how do I drain a node?": [chunk("runbook", "First, cordon the node.")],
                "what caused INC-2451?": [chunk("other", "unrelated text")],
            }
        )
        report = await run_benchmark(dataset(), retriever, tenant_id="benchmark")

        assert report.metrics.cases == 2
        assert report.metrics.answerable_rate.mean == pytest.approx(0.5)

    async def test_the_cases_that_found_nothing_are_listed(self) -> None:
        """An aggregate that moved says something changed; this says what."""
        retriever = ScriptedRetriever(
            {"how do I drain a node?": [chunk("runbook", "First, cordon the node.")]}
        )
        report = await run_benchmark(dataset(), retriever, tenant_id="benchmark")

        assert [case.outcome.case_id for case in report.failures] == ["q2"]

    async def test_the_configuration_travels_with_the_numbers(self) -> None:
        """A metric without the configuration that produced it cannot be compared
        with anything."""
        report = await run_benchmark(
            dataset(), ScriptedRetriever({}), tenant_id="t", configuration="chunk=512 rrf=60"
        )
        assert report.configuration == "chunk=512 rrf=60"
        assert report.dataset == "test-set"

    async def test_latency_is_recorded_per_case(self) -> None:
        report = await run_benchmark(dataset(), ScriptedRetriever({}), tenant_id="t")

        assert all(case.latency_ms >= 0 for case in report.cases)
        assert report.median_latency_ms >= 0


class TestEndToEndWithTheRealDataset:
    def test_the_committed_dataset_loads(self) -> None:
        """The dataset every measurement is relative to must at least parse, and
        CI is where that is worth finding out."""
        path = Path(__file__).resolve().parents[3].parent / "evaluation" / "datasets"
        dataset_file = path / "retrieval-v1.jsonl"
        if not dataset_file.exists():  # pragma: no cover - depends on checkout layout
            pytest.skip(f"{dataset_file} not present")

        loaded = EvaluationDataset.from_jsonl(dataset_file)

        assert len(loaded) >= 10
        assert all(case.supporting for case in loaded)


class TestComparingTwoRuns:
    """A benchmark exists to compare configurations, and that is a paired test."""

    @staticmethod
    def _retriever(*, hit: bool) -> ScriptedRetriever:
        found = [chunk("runbook", "cordon the node")]
        return ScriptedRetriever(
            {
                "how do I drain a node?": found if hit else [chunk("noise", "unrelated")],
                "what caused INC-2451?": [chunk("noise", "unrelated")],
            }
        )

    @staticmethod
    def _one_document() -> EvaluationDataset:
        """Two questions, both about the runbook: one cluster, not two."""
        return EvaluationDataset(
            name="one-document",
            cases=(
                EvaluationCase(
                    case_id="q1",
                    question="how do I drain a node?",
                    supporting=(SupportingPassage(document_id="runbook", quote="cordon the node"),),
                ),
                EvaluationCase(
                    case_id="q2",
                    question="what caused INC-2451?",
                    supporting=(SupportingPassage(document_id="runbook", quote="pool exhaustion"),),
                ),
            ),
        )

    async def test_two_runs_of_the_same_dataset_can_be_compared(self) -> None:
        better = await run_benchmark(
            dataset(), self._retriever(hit=True), tenant_id="benchmark", configuration="better"
        )
        worse = await run_benchmark(
            dataset(), self._retriever(hit=False), tenant_id="benchmark", configuration="worse"
        )
        difference = better.compare(worse, "answerable_rate")
        assert difference.mean > 0
        assert difference.n == 2

    async def test_comparing_different_datasets_is_refused(self) -> None:
        # Pairing scores from different questions is a wrong number with a
        # confident interval around it.
        first = await run_benchmark(
            dataset(), self._retriever(hit=True), tenant_id="benchmark", configuration="x"
        )
        second = await run_benchmark(
            self._one_document(),
            self._retriever(hit=True),
            tenant_id="benchmark",
            configuration="y",
        )
        with pytest.raises(ValueError, match="different datasets"):
            first.compare(second)

    async def test_an_unknown_metric_names_the_ones_that_exist(self) -> None:
        report = await run_benchmark(
            dataset(), self._retriever(hit=True), tenant_id="benchmark", configuration="x"
        )
        with pytest.raises(ValueError, match="ndcg_at_k"):
            report.compare(report, "made_up")

    async def test_questions_about_one_document_form_one_cluster(self) -> None:
        # So the intervals do not claim an independence the dataset does not have.
        report = await run_benchmark(
            self._one_document(),
            self._retriever(hit=True),
            tenant_id="benchmark",
            configuration="x",
        )
        assert set(report.clusters) == {"runbook"}
        assert report.metrics.recall_at_k.clusters == 1

    async def test_two_documents_are_two_clusters(self) -> None:
        report = await run_benchmark(
            dataset(), self._retriever(hit=True), tenant_id="benchmark", configuration="x"
        )
        assert report.metrics.recall_at_k.clusters == 2
