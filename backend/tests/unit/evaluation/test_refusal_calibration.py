"""Picking what a person labels, and scoring the judge against what they said.

The judge decides whether a response declines to answer. Until somebody checks
it, that is a figure and not a measurement (ADR-0032), and what gets checked has
to be chosen carefully: the question the judge exists to settle lives almost
entirely in the sixth of the dataset that cannot be answered, and a random
sample would mostly miss it.
"""

import json
from pathlib import Path

from paimon.evaluation.agent_benchmark import run_agent_benchmark
from paimon.evaluation.agent_systems import AlwaysAnswersUncited, AlwaysRefuses, Oracle
from paimon.evaluation.calibration import ACCEPTABLE_KAPPA, HumanLabel, load_labels
from paimon.evaluation.judging import Judgement, Verdict
from paimon.evaluation.refusal_calibration import (
    attempt_id,
    calibrate,
    instructions,
    stratified_sample,
    template,
    write_template,
)
from tests.unit.evaluation.test_agent_grading import DOCS, TASKS

NO_MATERIAL_TEXT = "I have no indexed material that bears on that question, so I cannot answer it."


def declines(_answer: str) -> Judgement:
    return Judgement(verdict=Verdict.YES, reasoning="it declines", model_id="judge")


def answers(_answer: str) -> Judgement:
    return Judgement(verdict=Verdict.NO, reasoning="it answers", model_id="judge")


async def reports() -> list[object]:
    """Three systems over the same dataset, as a comparison produces them."""
    return [
        await run_agent_benchmark(
            TASKS, Oracle(DOCS, NO_MATERIAL_TEXT), DOCS, trials=2, judge_refusal=answers
        ),
        await run_agent_benchmark(
            TASKS, AlwaysRefuses(NO_MATERIAL_TEXT), DOCS, trials=2, judge_refusal=declines
        ),
        await run_agent_benchmark(
            TASKS, AlwaysAnswersUncited(), DOCS, trials=2, judge_refusal=answers
        ),
    ]


class TestChoosingTheSample:
    async def test_it_takes_from_every_system(self) -> None:
        sample = stratified_sample(await reports(), per_cell=2)  # type: ignore[arg-type]

        assert {item.system for item in sample} == {
            "oracle",
            "always-refuses",
            "always-answers-uncited",
        }

    async def test_it_takes_from_every_category(self) -> None:
        # The point of stratifying. Four fifths of the dataset is answerable, so
        # a random sample would be four fifths answerable — and the question the
        # judge exists to settle lives in the other fifth.
        sample = stratified_sample(await reports(), per_cell=2)  # type: ignore[arg-type]

        assert set(TASKS.categories) <= {item.category for item in sample}

    async def test_the_size_follows_the_cells(self) -> None:
        # Three systems, five categories, two each.
        sample = stratified_sample(await reports(), per_cell=2)  # type: ignore[arg-type]

        assert len(sample) == 3 * len(TASKS.categories) * 2

    async def test_a_bigger_sample_is_available(self) -> None:
        sample = stratified_sample(await reports(), per_cell=4)  # type: ignore[arg-type]

        assert len(sample) == 3 * len(TASKS.categories) * 4

    async def test_it_is_reproducible(self) -> None:
        # A calibration sample nobody else can rebuild cannot be re-labelled by
        # a second person, and a second labeller is the only way to find out
        # whether the first was unusual.
        built = await reports()

        first = stratified_sample(built, per_cell=2)  # type: ignore[arg-type]
        second = stratified_sample(built, per_cell=2)  # type: ignore[arg-type]

        assert [item.case_id for item in first] == [item.case_id for item in second]

    async def test_every_case_id_is_distinct(self) -> None:
        # Two systems attempting one task must not collide, or one rater's
        # label would be scored against the other system's response.
        sample = stratified_sample(await reports(), per_cell=3)  # type: ignore[arg-type]

        assert len({item.case_id for item in sample}) == len(sample)

    def test_an_identity_names_system_task_and_trial(self) -> None:
        assert attempt_id("investigator", "a024", 3) == "investigator/a024/3"


class TestTheTemplate:
    async def test_it_carries_the_question_and_the_response(self) -> None:
        sample = stratified_sample(await reports(), per_cell=1)  # type: ignore[arg-type]

        rows = [json.loads(line) for line in template(sample).splitlines()]

        assert all(row["question"] and row["response"] for row in rows)

    async def test_the_verdict_is_blank(self) -> None:
        sample = stratified_sample(await reports(), per_cell=1)  # type: ignore[arg-type]

        rows = [json.loads(line) for line in template(sample).splitlines()]

        assert all(row["refusal"] == "" for row in rows)

    async def test_it_does_not_show_the_judges_verdict(self) -> None:
        # Showing it is the documented way to turn an independent measurement
        # into an expensive confirmation of what the model already said.
        sample = stratified_sample(await reports(), per_cell=1)  # type: ignore[arg-type]

        rows = [json.loads(line) for line in template(sample).splitlines()]

        assert all("judged" not in row for row in rows)

    async def test_it_does_not_show_the_category_or_the_system(self) -> None:
        # Either would tell the labeller what to expect. "out-of-corpus" is the
        # expected answer written on the question paper.
        sample = stratified_sample(await reports(), per_cell=1)  # type: ignore[arg-type]

        rows = [json.loads(line) for line in template(sample).splitlines()]

        assert all("category" not in row and "system" not in row for row in rows)

    async def test_it_round_trips_through_the_label_loader(self, tmp_path: Path) -> None:
        sample = stratified_sample(await reports(), per_cell=1)  # type: ignore[arg-type]
        path = tmp_path / "labels.jsonl"
        write_template(sample, path)
        filled = "\n".join(
            json.dumps({**json.loads(line), "refusal": "yes"})
            for line in path.read_text(encoding="utf-8").splitlines()
        )
        path.write_text(filled + "\n", encoding="utf-8")

        labels = load_labels(path)

        assert len(labels) == len(sample)
        assert all(label.refusal is Verdict.YES for label in labels)

    async def test_the_instructions_are_written_beside_it(self, tmp_path: Path) -> None:
        sample = stratified_sample(await reports(), per_cell=1)  # type: ignore[arg-type]
        path = tmp_path / "labels.jsonl"

        write_template(sample, path)

        assert "yes" in path.with_suffix(".md").read_text(encoding="utf-8")

    def test_the_instructions_repeat_the_rubric_the_judge_was_given(self) -> None:
        # The two raters must be answering the same question, or kappa measures
        # a disagreement about the task rather than about the responses.
        text = instructions()

        assert "declines" in text
        assert "Citations do not make a response an answer" in text


class TestScoringTheJudge:
    async def test_perfect_agreement_is_kappa_one(self) -> None:
        sample = stratified_sample(await reports(), per_cell=2)  # type: ignore[arg-type]
        labels = [HumanLabel(case_id=item.case_id, refusal=item.judged) for item in sample]

        result = calibrate(sample, labels)

        assert result.kappa == 1.0
        assert result.compared == len(sample)

    async def test_total_disagreement_is_not(self) -> None:
        sample = stratified_sample(await reports(), per_cell=2)  # type: ignore[arg-type]
        flipped = [
            HumanLabel(
                case_id=item.case_id,
                refusal=Verdict.NO if item.judged is Verdict.YES else Verdict.YES,
            )
            for item in sample
        ]

        result = calibrate(sample, flipped)

        assert result.kappa < ACCEPTABLE_KAPPA

    async def test_unlabelled_rows_are_not_counted_against_either_rater(self) -> None:
        # A partly finished file measures what it covers, which is the truth.
        sample = stratified_sample(await reports(), per_cell=2)  # type: ignore[arg-type]
        labels = [HumanLabel(case_id=item.case_id, refusal=item.judged) for item in sample[:5]]

        result = calibrate(sample, labels)

        assert result.compared == 5
