"""Picking what a person labels, and scoring the judge against what they said.

The judge decides whether a response declines to answer. Until somebody checks
it, that is a figure and not a measurement (ADR-0032), and what gets checked has
to be chosen carefully: the question the judge exists to settle lives almost
entirely in the sixth of the dataset that cannot be answered, and a random
sample would mostly miss it.
"""

import json
from dataclasses import asdict, replace
from pathlib import Path

from paimon.evaluation.agent_benchmark import run_agent_benchmark
from paimon.evaluation.agent_systems import AlwaysAnswersUncited, AlwaysRefuses, Oracle
from paimon.evaluation.calibration import ACCEPTABLE_KAPPA, HumanLabel, load_labels
from paimon.evaluation.judging import Judgement, Verdict
from paimon.evaluation.refusal_calibration import (
    SampledAttempt,
    attempt_id,
    attempts_from_report,
    calibrate,
    instructions,
    opaque_ids,
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


async def pool() -> list[SampledAttempt]:
    """Every attempt from three systems, as a calibration sample is taken from.

    Through ``asdict`` and back, because that is the shape a stored report has
    and the sample must be takeable from a report written before any judge ran.
    """
    systems = (
        Oracle(DOCS, NO_MATERIAL_TEXT),
        AlwaysRefuses(NO_MATERIAL_TEXT),
        AlwaysAnswersUncited(),
    )
    found: list[SampledAttempt] = []
    for system in systems:
        report = await run_agent_benchmark(TASKS, system, DOCS, trials=2)
        found += attempts_from_report(asdict(report))
    return found


class TestChoosingTheSample:
    async def test_it_takes_from_every_system(self) -> None:
        sample = stratified_sample(await pool(), per_cell=2)

        assert {item.system for item in sample} == {
            "oracle",
            "always-refuses",
            "always-answers-uncited",
        }

    async def test_it_takes_from_every_category(self) -> None:
        # The point of stratifying. Four fifths of the dataset is answerable, so
        # a random sample would be four fifths answerable — and the question the
        # judge exists to settle lives in the other fifth.
        sample = stratified_sample(await pool(), per_cell=2)

        assert set(TASKS.categories) <= {item.category for item in sample}

    async def test_the_size_follows_the_cells(self) -> None:
        # Three systems, five categories, two each.
        sample = stratified_sample(await pool(), per_cell=2)

        assert len(sample) == 3 * len(TASKS.categories) * 2

    async def test_a_bigger_sample_is_available(self) -> None:
        sample = stratified_sample(await pool(), per_cell=4)

        assert len(sample) == 3 * len(TASKS.categories) * 4

    async def test_named_cases_are_included_whatever_the_strata_say(self) -> None:
        # The attempts a grader and a person are known to disagree about have
        # to be in the sample, or the calibration measures agreement on the
        # easy cases and says nothing about the contested ones.
        everything = await pool()
        wanted = [everything[7].case_id, everything[40].case_id]

        sample = stratified_sample(everything, per_cell=1, must_include=wanted)

        assert set(wanted) <= {item.case_id for item in sample}

    async def test_a_named_case_is_not_marked_as_one(self) -> None:
        # A labeller who can tell which rows are contested labels those more
        # carefully, and a kappa over rows rated with two levels of care
        # measures attention rather than the judge.
        everything = await pool()
        wanted = [everything[7].case_id]

        rendered = template(stratified_sample(everything, per_cell=1, must_include=wanted))

        assert "disputed" not in rendered
        assert "must_include" not in rendered

    async def test_it_can_be_trimmed_to_a_size(self) -> None:
        sample = stratified_sample(await pool(), per_cell=4, size=12)

        assert len(sample) == 12

    async def test_it_is_reproducible(self) -> None:
        # A calibration sample nobody else can rebuild cannot be re-labelled by
        # a second person, and a second labeller is the only way to find out
        # whether the first was unusual.
        built = await pool()

        first = stratified_sample(built, per_cell=2)
        second = stratified_sample(built, per_cell=2)

        assert [item.case_id for item in first] == [item.case_id for item in second]

    async def test_every_case_id_is_distinct(self) -> None:
        # Two systems attempting one task must not collide, or one rater's
        # label would be scored against the other system's response.
        sample = stratified_sample(await pool(), per_cell=3)

        assert len({item.case_id for item in sample}) == len(sample)

    def test_an_identity_names_system_task_and_trial(self) -> None:
        assert attempt_id("investigator", "a024", 3) == "investigator/a024/3"


class TestTheTemplate:
    async def test_it_carries_the_response(self) -> None:
        sample = stratified_sample(await pool(), per_cell=1)

        rows = [json.loads(line) for line in template(sample).splitlines()]

        assert all(row["response"] for row in rows)

    async def test_it_does_not_carry_the_question(self) -> None:
        # The judge is not shown it either. Kappa compares two raters, which
        # means nothing unless they were asked the same thing — and a person
        # holding the question answers the harder one, "did this answer it
        # well", which is what made the judge call a correct answer a refusal.
        sample = stratified_sample(await pool(), per_cell=1)

        rows = [json.loads(line) for line in template(sample).splitlines()]

        assert all("question" not in row for row in rows)

    async def test_the_verdict_is_blank(self) -> None:
        sample = stratified_sample(await pool(), per_cell=1)

        rows = [json.loads(line) for line in template(sample).splitlines()]

        assert all(row["refusal"] == "" for row in rows)

    async def test_it_does_not_show_the_judges_verdict(self) -> None:
        # Showing it is the documented way to turn an independent measurement
        # into an expensive confirmation of what the model already said.
        sample = stratified_sample(await pool(), per_cell=1)

        rows = [json.loads(line) for line in template(sample).splitlines()]

        assert all("judged" not in row for row in rows)

    async def test_it_does_not_show_the_category_or_the_system(self) -> None:
        # Either would tell the labeller what to expect. "out-of-corpus" is the
        # expected answer written on the question paper.
        sample = stratified_sample(await pool(), per_cell=1)

        rows = [json.loads(line) for line in template(sample).splitlines()]

        assert all("category" not in row and "system" not in row for row in rows)

    async def test_the_identifier_gives_nothing_away_either(self) -> None:
        # system/task/trial is readable on purpose and unusable here: it names
        # the harness, and it names the task, which tells whoever wrote the
        # dataset what category it is.
        sample = stratified_sample(await pool(), per_cell=2)

        rendered = template(sample)

        for leak in ("oracle", "always-refuses", "always-answers-uncited"):
            assert leak not in rendered
        assert all(task.task_id not in rendered for task in TASKS)

    async def test_it_round_trips_through_the_label_loader(self, tmp_path: Path) -> None:
        sample = stratified_sample(await pool(), per_cell=1)
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
        sample = stratified_sample(await pool(), per_cell=1)
        path = tmp_path / "labels.jsonl"

        write_template(sample, path)

        assert "yes" in path.with_suffix(".md").read_text(encoding="utf-8")

    async def test_the_key_is_written_somewhere_else(self, tmp_path: Path) -> None:
        # It turns c17 back into a system and a task. A labeller holding it can
        # look up what every row came from.
        sample = stratified_sample(await pool(), per_cell=1)
        path = tmp_path / "labels.jsonl"
        key = tmp_path / "keys" / "key.jsonl"

        write_template(sample, path, key=key)

        assert key.exists()
        assert key.parent != path.parent
        assert len(key.read_text(encoding="utf-8").strip().splitlines()) == len(sample)

    async def test_without_a_key_path_nothing_maps_back(self, tmp_path: Path) -> None:
        sample = stratified_sample(await pool(), per_cell=1)
        path = tmp_path / "labels.jsonl"

        write_template(sample, path)

        assert list(tmp_path.glob("*key*")) == []  # noqa: ASYNC240  a test, not a request path

    def test_the_instructions_repeat_the_rubric_the_judge_was_given(self) -> None:
        # The two raters must be answering the same question, or kappa measures
        # a disagreement about the task rather than about the responses.
        text = instructions()

        assert "declines" in text
        assert "Citations do not make a text an answer" in text

    def test_the_instructions_offer_two_labels_and_no_third(self) -> None:
        text = instructions()

        assert "two labels and no third" in text
        assert '"partial"' not in text


def as_judged(sample: list[SampledAttempt]) -> list[SampledAttempt]:
    """Put a verdict on each sampled attempt, as a judged run would."""
    return [
        replace(item, judged=Verdict.YES if "not" in item.answer else Verdict.NO) for item in sample
    ]


class TestScoringTheJudge:
    async def test_perfect_agreement_is_kappa_one(self) -> None:
        sample = as_judged(stratified_sample(await pool(), per_cell=2))
        naming = opaque_ids(sample)
        labels = [HumanLabel(case_id=naming[item.case_id], refusal=item.judged) for item in sample]

        result = calibrate(sample, labels)

        assert result.kappa == 1.0
        assert result.compared == len(sample)

    async def test_total_disagreement_is_not(self) -> None:
        sample = as_judged(stratified_sample(await pool(), per_cell=2))
        naming = opaque_ids(sample)
        flipped = [
            HumanLabel(
                case_id=naming[item.case_id],
                refusal=Verdict.NO if item.judged is Verdict.YES else Verdict.YES,
            )
            for item in sample
        ]

        result = calibrate(sample, flipped)

        assert result.kappa < ACCEPTABLE_KAPPA

    async def test_unlabelled_rows_are_not_counted_against_either_rater(self) -> None:
        # A partly finished file measures what it covers, which is the truth.
        sample = as_judged(stratified_sample(await pool(), per_cell=2))
        naming = opaque_ids(sample)
        labels = [
            HumanLabel(case_id=naming[item.case_id], refusal=item.judged) for item in sample[:5]
        ]

        result = calibrate(sample, labels)

        assert result.compared == 5
