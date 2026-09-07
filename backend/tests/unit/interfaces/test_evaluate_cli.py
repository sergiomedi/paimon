"""What the benchmark command prints, and what it refuses to.

The rendering is the part a person reads and therefore the part that decides
what they conclude. A number without its interval invites a comparison; these
tests fix that it never appears alone.
"""

import json
from dataclasses import asdict
from pathlib import Path

import pytest
from tests.unit.evaluation.test_runner import ScriptedRetriever, chunk

from paimon.domain.entities import Chunk
from paimon.evaluation import (
    EvaluationCase,
    EvaluationDataset,
    SupportingPassage,
    run_benchmark,
)
from paimon.evaluation.runner import BenchmarkReport
from paimon.interfaces.cli.evaluate import load_report, render, render_comparison

TENANT = "benchmark"


def dataset(name: str = "test-set") -> EvaluationDataset:
    """Four questions across two documents, so clustering has something to do."""
    return EvaluationDataset(
        name=name,
        cases=tuple(
            EvaluationCase(
                case_id=f"q{index}",
                question=f"question {index}?",
                supporting=(
                    SupportingPassage(
                        document_id="runbook" if index < 3 else "postmortem",
                        quote="cordon the node",
                    ),
                ),
            )
            for index in range(1, 5)
        ),
    )


def retriever(*, found: int) -> ScriptedRetriever:
    """Answers the first ``found`` questions correctly and the rest with noise.

    A correct answer has to come from the document the case names, not merely
    contain the right words: ground truth is anchored to a quotation *in a
    document* (ADR-0013).
    """
    miss = [chunk("noise", "unrelated")]
    answers: dict[str, list[Chunk]] = {}
    for index, case in enumerate(dataset(), start=1):
        passage = case.supporting[0]
        answers[case.question] = (
            [chunk(passage.document_id, passage.quote)] if index <= found else miss
        )
    return ScriptedRetriever(answers)


async def benchmark(*, found: int, label: str, name: str = "test-set") -> BenchmarkReport:
    return await run_benchmark(
        dataset(name), retriever(found=found), tenant_id=TENANT, configuration=label
    )


class TestRendering:
    async def test_no_number_appears_without_its_interval(self) -> None:
        # The whole point. A reader who sees a bare percentage will compare two
        # of them and conclude something.
        rendered = render(await benchmark(found=3, label="tuned"))
        for line in rendered.splitlines():
            if any(metric in line for metric in ("recall@k", "precision@k", "nDCG@k", "MRR")):
                assert "±" in line

    async def test_it_says_how_many_independent_groups_there_were(self) -> None:
        # Four questions from two documents is not four independent observations,
        # and a reader has to be able to see that without reading the code.
        rendered = render(await benchmark(found=3, label="tuned"))
        assert "4 cases, 2 independent groups" in rendered

    async def test_it_names_the_configuration(self) -> None:
        assert "tuned" in render(await benchmark(found=3, label="tuned"))

    async def test_it_lists_the_questions_that_found_nothing(self) -> None:
        # The aggregate says something moved; this says what.
        rendered = render(await benchmark(found=1, label="tuned"))
        assert "retrieved nothing relevant:" in rendered
        assert "q4" in rendered


class TestComparing:
    async def test_a_real_improvement_is_reported_as_one(self) -> None:
        better = await benchmark(found=4, label="tuned")
        worse = await benchmark(found=0, label="baseline")
        rendered = render_comparison(better, worse)
        assert "distinguishable from zero" in rendered

    async def test_a_difference_inside_the_noise_says_so(self) -> None:
        better = await benchmark(found=3, label="tuned")
        worse = await benchmark(found=2, label="baseline")
        assert "not distinguishable from noise" in render_comparison(better, worse)

    async def test_it_reports_what_pairing_bought(self) -> None:
        better = await benchmark(found=4, label="tuned")
        worse = await benchmark(found=2, label="baseline")
        assert "agreed about which questions were hard" in render_comparison(better, worse)


class TestReadingABaseline:
    async def test_a_report_round_trips_through_json(self, tmp_path: Path) -> None:
        report = await benchmark(found=3, label="baseline")
        path = tmp_path / "baseline.json"
        path.write_text(json.dumps(asdict(report), indent=2), encoding="utf-8")

        loaded = load_report(path)
        assert loaded.dataset == report.dataset
        assert loaded.scores["ndcg_at_k"] == report.scores["ndcg_at_k"]

    def test_a_report_without_per_question_scores_is_refused(self, tmp_path: Path) -> None:
        # Older reports can be read for their aggregates and cannot be paired
        # against. Saying so beats approximating a comparison from two means.
        path = tmp_path / "old.json"
        path.write_text(
            json.dumps({"dataset": "test-set", "configuration": "old"}), encoding="utf-8"
        )
        with pytest.raises(ValueError, match="per-question scores"):
            load_report(path)
