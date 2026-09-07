"""What the benchmark command prints, and what it refuses to.

The rendering is the part a person reads and therefore the part that decides
what they conclude. A number without its interval invites a comparison; these
tests fix that it never appears alone.
"""

import json
from dataclasses import asdict
from pathlib import Path

import pytest
from tests.fakes import ScriptedJudge
from tests.unit.evaluation.test_answering import CORPUS, answerer
from tests.unit.evaluation.test_answering import dataset as answers_dataset
from tests.unit.evaluation.test_runner import ScriptedRetriever, chunk

from paimon.domain.entities import Chunk
from paimon.evaluation import (
    AnsweringReport,
    EvaluationCase,
    EvaluationDataset,
    HumanLabel,
    Judging,
    SupportingPassage,
    Verdict,
    labelling_template,
    run_answering_benchmark,
    run_benchmark,
)
from paimon.evaluation.runner import BenchmarkReport
from paimon.interfaces.cli.evaluate import (
    load_report,
    render,
    render_answers,
    render_comparison,
)

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


class TestRenderingAnAnsweringRun:
    """The answering report, which is verified rather than judged."""

    @staticmethod
    async def _run(*, faithful: bool) -> AnsweringReport:
        return await run_answering_benchmark(
            answers_dataset(),
            answerer(faithful=faithful),
            CORPUS,
            tenant_id=TENANT,
            configuration="local",
        )

    async def test_no_number_appears_without_its_interval(self) -> None:
        rendered = render_answers(await self._run(faithful=True))
        for line in rendered.splitlines():
            if any(name in line for name in ("grounded", "citation accuracy", "cited sentences")):
                assert "±" in line

    async def test_it_says_the_numbers_were_verified_not_judged(self) -> None:
        # The distinction is the whole design of this batch, so it is in the
        # output rather than only in an ADR nobody opens.
        rendered = render_answers(await self._run(faithful=True))
        assert "Verified, not judged" in rendered
        assert "No model graded this run" in rendered

    async def test_a_citation_that_did_not_survive_is_named(self) -> None:
        rendered = render_answers(await self._run(faithful=False))
        assert "citations that did not survive being followed:" in rendered
        assert "misquoted" in rendered
        assert "q2" in rendered

    async def test_a_clean_run_lists_nothing(self) -> None:
        assert "did not survive" not in render_answers(await self._run(faithful=True))


class TestRenderingTheJudgedSection:
    """A model's opinion has to look different from a verified number."""

    @staticmethod
    async def _judged(*, self_judged: bool = False, harsh: bool = False) -> AnsweringReport:
        return await run_answering_benchmark(
            answers_dataset(),
            answerer(faithful=True),
            CORPUS,
            tenant_id=TENANT,
            configuration="local",
            judging=Judging(
                judge=ScriptedJudge(
                    model_id="llama-judge",
                    default=Verdict.NO if harsh else Verdict.YES,
                ),
                self_judged=self_judged,
            ),
        )

    async def test_it_names_the_judge(self) -> None:
        # Two runs judged by different models are not comparable.
        assert "judged by llama-judge" in render_answers(await self._judged())

    async def test_it_carries_the_caveat_that_makes_the_number_readable(self) -> None:
        rendered = render_answers(await self._judged())
        assert "56%" in rendered
        assert "erred towards saying the answer was supported" in rendered

    async def test_self_judging_is_shouted_not_footnoted(self) -> None:
        rendered = render_answers(await self._judged(self_judged=True))
        assert "IT JUDGED ITSELF" in rendered

    async def test_a_run_without_a_judge_prints_no_judged_lines(self) -> None:
        unjudged = await run_answering_benchmark(
            answers_dataset(), answerer(faithful=True), CORPUS, tenant_id=TENANT
        )
        rendered = render_answers(unjudged)
        assert "judged by" not in rendered
        assert "faithfulness" not in rendered

    async def test_the_verified_section_still_says_it_was_verified(self) -> None:
        # Both sections appear, and the reader can tell them apart.
        rendered = render_answers(await self._judged())
        assert "Verified, not judged" in rendered
        assert "faithfulness" in rendered

    async def test_it_stops_claiming_nothing_was_graded_once_something_was(self) -> None:
        # The verified section says "no model graded this run" only when that is
        # true. With a judge present it would be a small lie, and a report that
        # tells small lies is not worth reading carefully.
        assert "No model graded this run" not in render_answers(await self._judged())


class TestTheCalibrationSection:
    """A judged number has to say whether anybody checked it."""

    @staticmethod
    async def _with_labels(labels: list[HumanLabel]) -> AnsweringReport:
        return await run_answering_benchmark(
            answers_dataset(),
            answerer(faithful=True),
            CORPUS,
            tenant_id=TENANT,
            judging=Judging(judge=ScriptedJudge(model_id="llama-judge"), labels=labels),
        )

    async def test_an_uncalibrated_judge_says_so_loudly(self) -> None:
        report = await run_answering_benchmark(
            answers_dataset(),
            answerer(faithful=True),
            CORPUS,
            tenant_id=TENANT,
            judging=Judging(judge=ScriptedJudge()),
        )
        rendered = render_answers(report)
        assert "UNCALIBRATED" in rendered
        assert "a figure rather than a measurement" in rendered

    async def test_agreement_is_reported_when_labels_exist(self) -> None:
        rendered = render_answers(
            await self._with_labels(
                [
                    HumanLabel(case_id="q1", faithfulness=Verdict.YES, relevance=Verdict.YES),
                    HumanLabel(case_id="q2", faithfulness=Verdict.YES, relevance=Verdict.YES),
                ]
            )
        )
        assert "calibrated against 2 human labels" in rendered
        assert "UNCALIBRATED" not in rendered
        assert "kappa" in rendered

    async def test_a_disagreeing_judge_is_told_not_to_be_quoted(self) -> None:
        rendered = render_answers(
            await self._with_labels(
                [
                    HumanLabel(case_id="q1", faithfulness=Verdict.NO, relevance=Verdict.NO),
                    HumanLabel(case_id="q2", faithfulness=Verdict.NO, relevance=Verdict.NO),
                ]
            )
        )
        assert "TOO LOW" in rendered
        assert "an indication, not a result" in rendered

    def test_the_labelling_template_carries_what_a_labeller_needs(self, tmp_path: Path) -> None:
        # Question, answer and expected passages on one line, so labelling is
        # reading a line rather than cross-referencing three files.
        rendered = labelling_template(
            [
                {
                    "case_id": "q1",
                    "question": "how do I drain a node?",
                    "answer": "Cordon it [1].",
                    "expected_passages": ["Cordon the node"],
                    "faithfulness": "",
                    "relevance": "",
                }
            ]
        )
        row = json.loads(rendered.strip())
        assert row["expected_passages"] == ["Cordon the node"]
        assert row["faithfulness"] == ""
