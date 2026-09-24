"""Picking what a person labels, and scoring the judge against what they said.

The judge decides whether a response declines to answer. Until somebody checks
it, that is a figure and not a measurement (ADR-0032), and what gets checked has
to be chosen carefully: the question the judge exists to settle lives almost
entirely in the sixth of the dataset that cannot be answered, and a random
sample would mostly miss it.
"""

import json
from collections import Counter
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from paimon.evaluation.agent_benchmark import run_agent_benchmark
from paimon.evaluation.agent_grading import REFUSALS
from paimon.evaluation.agent_systems import AlwaysAnswersUncited, AlwaysRefuses, Oracle
from paimon.evaluation.calibration import ACCEPTABLE_KAPPA, HumanLabel, load_labels
from paimon.evaluation.judging import REFUSAL_QUESTION, REFUSAL_RUBRIC, Judgement, Verdict
from paimon.evaluation.refusal_calibration import (
    SampledAttempt,
    attempt_id,
    attempts_from_report,
    audit_cell,
    calibrate,
    distinct_texts,
    instructions,
    opaque_ids,
    stratified_sample,
    template,
    without_canned_refusals,
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


class TestWhatIsWorthLabelling:
    """Two filters, because sixty rows of a person's attention is the budget."""

    async def test_repeated_texts_are_collapsed(self) -> None:
        # At temperature zero a system answers the same task the same way five
        # times. A sample of sixty attempts held forty-one distinct texts, and
        # kappa over a text labelled five times counts one judgement five times.
        everything = await pool()

        unique = distinct_texts(everything)

        rendered = {" ".join(item.answer.split()) for item in unique}
        assert len(rendered) == len(unique)
        assert len(unique) < len(everything)

    async def test_the_first_occurrence_is_the_one_kept(self) -> None:
        everything = await pool()

        unique = distinct_texts(everything)

        assert unique[0].case_id == everything[0].case_id

    async def test_canned_refusals_are_dropped(self) -> None:
        # Code grades these by equality against the constants that define them,
        # so a judge classifying them is measured on work it was never given.
        everything = await pool()

        kept = without_canned_refusals(everything, REFUSALS)

        assert all(item.answer.strip() not in REFUSALS for item in kept)
        assert len(kept) < len(everything)

    async def test_dropping_them_also_hides_which_system_it_was(self) -> None:
        # Only the agents emit them. A labeller who recognises one is no longer
        # rating the text alone.
        everything = await pool()

        rendered = template(without_canned_refusals(everything, REFUSALS))

        assert NO_MATERIAL_TEXT not in rendered

    async def test_a_named_case_survives_the_trim(self) -> None:
        # A sample that dropped the cases it was built around would measure
        # agreement on the easy ones and say nothing about the contested ones.
        everything = distinct_texts(await pool())
        wanted = [everything[3].case_id, everything[30].case_id]

        sample = stratified_sample(everything, per_cell=6, must_include=wanted, size=5)

        assert len(sample) == 5
        assert set(wanted) <= {item.case_id for item in sample}

    async def test_a_sample_of_distinct_texts_stays_distinct(self) -> None:
        everything = distinct_texts(await pool())

        sample = stratified_sample(everything, per_cell=6, size=20)

        assert len({" ".join(item.answer.split()) for item in sample}) == len(sample)

    async def test_the_trim_does_not_favour_the_alphabet(self) -> None:
        # The cells are visited in sorted order, so trimming the fill as it
        # stands empties the end of the alphabet: the first version of this
        # left three `investigator` rows in sixty of the real sample, having
        # filled the budget from `answers` and `incident-triage` first.
        #
        # Asserted as a share of what was available rather than as a count,
        # because a system with few distinct texts should contribute few rows —
        # that is the sample working, not the trim misbehaving.
        everything = distinct_texts(await pool())
        available = Counter(item.system for item in everything)

        sample = stratified_sample(everything, per_cell=6, size=30)

        taken = Counter(item.system for item in sample)
        for system, count in available.items():
            pool_share = count / len(everything)
            sample_share = taken[system] / len(sample)
            assert sample_share >= pool_share / 2, f"{system} under-represented"


class TestBothRatersAreAskedTheSameThing:
    """Kappa compares two raters. It cannot tell "worse" from "asked differently".

    The first version wrote the rules twice, and they drifted by exactly one:
    *a confidently wrong answer is an answer* reached the labeller and not the
    judge. The judge then graded a fabricated root cause — for an incident that
    does not exist — as a refusal, which on an out-of-corpus task turns a
    hallucination into a pass. One missing rule, the one error that matters.
    """

    def test_the_labeller_gets_the_judges_question(self) -> None:
        assert REFUSAL_QUESTION in instructions()

    def test_the_judge_gets_it_too(self) -> None:
        assert REFUSAL_QUESTION in REFUSAL_RUBRIC

    @pytest.mark.parametrize(
        "rule",
        [
            "Citations do not make a text an answer",
            "A short answer is an answer",
            "A hedged answer is an answer",
            "incomplete is still an answer",
            "A confidently wrong answer is an answer",
            "two labels and no third",
            "not shown the question",
        ],
    )
    def test_every_rule_reaches_both(self, rule: str) -> None:
        assert rule in REFUSAL_RUBRIC
        assert rule in instructions()

    def test_only_the_mechanics_differ(self) -> None:
        # The judge replies in JSON and is pushed off the fence; a person fills
        # in a field and may leave a row blank. A blank costs one case from the
        # comparison, which is cheaper than a guess recorded as a judgement.
        assert "JSON" in REFUSAL_RUBRIC
        assert "JSON" not in instructions()
        assert "Leave a line blank" in instructions()
        assert "Leave a line blank" not in REFUSAL_RUBRIC


def graded(system: str, *tasks: dict[str, object]) -> dict[str, object]:
    """A graded report, shaped the way the agent benchmark writes one."""
    return {"system": system, "tasks": list(tasks)}


def task(task_id: str, category: str, *rows: tuple[str, str | None, str]) -> dict[str, object]:
    """One task with its attempts and the grades that sit positionally beside them."""
    return {
        "task_id": task_id,
        "category": category,
        "question": "who do I call?",
        "attempts": [
            {"task_id": task_id, "trial": index, "text": text, "failed": failed}
            for index, (text, _, failed) in enumerate(rows, start=1)
        ],
        "grades": [{"judge_verdict": verdict} for _, verdict, _ in rows],
    }


class TestAuditingTheExpensiveCell:
    """Reading one cell in full, rather than sampling it and setting a threshold.

    The error worth spending a person on — an answer to a question the corpus
    cannot answer, recorded as a refusal — can only happen in one cell, and
    that cell is small enough to read entirely.
    """

    def test_it_takes_every_attempt_in_the_category(self) -> None:
        report = graded(
            "answers",
            task("a023", "out-of-corpus", ("nothing covers this", "yes", "")),
            task("a001", "one-hop", ("cordon it", "no", "")),
            task("a024", "out-of-corpus", ("I cannot find it", "yes", "")),
        )

        found = audit_cell(report)

        assert [item.case_id for item in found] == ["answers/a023/1", "answers/a024/1"]

    def test_it_carries_the_judge_verdict_for_scoring(self) -> None:
        report = graded(
            "answers", task("a023", "out-of-corpus", ("nothing covers this", "yes", ""))
        )

        assert audit_cell(report)[0].judged is Verdict.YES

    def test_the_verdict_never_reaches_the_labelling_file(self) -> None:
        # The whole point of the audit is a second opinion. A file that carries
        # the verdict under review is a file that collects agreement with it.
        report = graded(
            "answers",
            task("a023", "out-of-corpus", ("the sources do not cover INC-4102", "yes", "")),
        )

        rendered = template(audit_cell(report))

        assert "yes" not in json.loads(rendered.splitlines()[0]).values()
        assert json.loads(rendered.splitlines()[0])["refusal"] == ""
        assert "answers" not in rendered
        assert "a023" not in rendered

    def test_attempts_that_never_ran_are_left_out(self) -> None:
        report = graded(
            "answers",
            task("a023", "out-of-corpus", ("", None, "connection reset"), ("no idea", "yes", "")),
        )

        assert [item.case_id for item in audit_cell(report)] == ["answers/a023/2"]

    def test_the_whole_category_is_taken_not_just_the_refusals(self) -> None:
        # A file in which every row is a judged refusal tells its labeller so,
        # however opaque the ids are. Taking the whole cell removes the anchor
        # and catches the opposite error, which a one-sided audit cannot see.
        report = graded(
            "answers",
            task(
                "a023",
                "out-of-corpus",
                ("nothing covers this", "yes", ""),
                ("the ceiling is 9000", "no", ""),
            ),
        )

        found = audit_cell(report)

        assert [item.judged for item in found] == [Verdict.YES, Verdict.NO]

    def test_one_sided_is_available_when_asked_for(self) -> None:
        report = graded(
            "answers",
            task(
                "a023",
                "out-of-corpus",
                ("nothing covers this", "yes", ""),
                ("the ceiling is 9000", "no", ""),
            ),
        )

        found = audit_cell(report, judged_only=True)

        assert [item.case_id for item in found] == ["answers/a023/1"]
