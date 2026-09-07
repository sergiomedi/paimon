"""Measuring the judge against a person.

The point of this module is that a judge's agreement with the published
literature is not the same as its agreement with *you*, on *your* corpus, with
*your* rubric. These tests fix the arithmetic that makes that difference visible
— and in particular that raw agreement alone is not allowed to stand in for it.
"""

import json
from pathlib import Path

import pytest

from paimon.evaluation.calibration import (
    ACCEPTABLE_KAPPA,
    agreement,
    cohens_kappa,
    labelling_template,
    load_labels,
)
from paimon.evaluation.judging import Verdict

YES, PARTIAL, NO = Verdict.YES, Verdict.PARTIAL, Verdict.NO


class TestKappa:
    def test_perfect_agreement_is_one(self) -> None:
        assert cohens_kappa([YES, NO, PARTIAL], [YES, NO, PARTIAL]) == pytest.approx(1.0)

    def test_agreeing_only_by_chance_is_about_zero(self) -> None:
        # The reason kappa exists. Two raters who both say "yes" most of the time
        # agree often and have measured nothing about each other.
        judge = [YES, YES, YES, NO]
        human = [YES, YES, NO, YES]
        assert abs(cohens_kappa(judge, human)) < 0.5

    def test_a_rater_that_always_says_yes_scores_zero_not_ninety_percent(self) -> None:
        # The failure raw agreement hides: on a skewed dataset, a judge that says
        # "yes" to everything agrees 90% of the time.
        judge = [YES] * 10
        human = [YES] * 9 + [NO]
        assert cohens_kappa(judge, human) == pytest.approx(0.0, abs=1e-9)

    def test_worse_than_chance_is_negative(self) -> None:
        assert cohens_kappa([YES, NO], [NO, YES]) < 0

    def test_two_raters_who_used_one_label_and_the_same_one_agreed(self) -> None:
        # Chance agreement is already total, so the formula is undefined. They
        # did agree on everything, and reporting zero would say the opposite.
        assert cohens_kappa([YES, YES], [YES, YES]) == 1.0

    def test_mismatched_lengths_are_refused(self) -> None:
        with pytest.raises(ValueError, match="same cases"):
            cohens_kappa([YES], [YES, NO])

    def test_no_cases_is_zero(self) -> None:
        assert cohens_kappa([], []) == 0.0


class TestAgreement:
    def test_it_compares_only_the_cases_both_rated(self) -> None:
        measured = agreement(
            {"q1": YES, "q2": NO, "q3": YES},
            {"q1": YES, "q2": NO},
        )
        assert measured.compared == 2
        assert measured.raw.mean == 1.0

    def test_an_abstention_is_skipped_not_counted_as_a_disagreement(self) -> None:
        # "The judge broke" and "the judge was wrong" are different facts, and
        # only one of them is about its accuracy.
        measured = agreement(
            {"q1": YES, "q2": Verdict.UNDECIDED},
            {"q1": YES, "q2": NO},
        )
        assert measured.compared == 1
        assert measured.skipped == 1
        assert measured.raw.mean == 1.0

    def test_raw_agreement_carries_an_interval(self) -> None:
        # Fifteen labels produce an agreement figure that moves several points on
        # nothing, exactly as the retrieval metrics do (ADR-0029).
        measured = agreement(
            {f"q{i}": YES for i in range(10)},
            {f"q{i}": (YES if i < 8 else NO) for i in range(10)},
        )
        low, high = measured.raw.interval()
        assert low < measured.raw.mean < high

    def test_a_low_kappa_is_reported_as_unacceptable(self) -> None:
        measured = agreement(
            {f"q{i}": YES for i in range(10)},
            {f"q{i}": (YES if i < 9 else NO) for i in range(10)},
        )
        assert measured.kappa < ACCEPTABLE_KAPPA
        assert not measured.is_acceptable
        assert "TOO LOW" in measured.format()

    def test_nothing_comparable_says_so(self) -> None:
        measured = agreement({}, {"q1": YES})
        assert measured.compared == 0
        assert "no cases could be compared" in measured.format()


class TestTheLabellingFile:
    def test_the_template_does_not_show_the_judge_s_verdict(self) -> None:
        # Showing it would anchor the labeller, turning an independent
        # measurement into an expensive confirmation of what the model said.
        rendered = labelling_template(
            [{"case_id": "q1", "question": "why?", "answer": "because [1]."}]
        )
        assert "verdict" not in rendered
        assert "judge" not in rendered

    def test_a_completed_file_is_read(self, tmp_path: Path) -> None:
        path = tmp_path / "labels.jsonl"
        path.write_text(
            json.dumps({"case_id": "q1", "faithfulness": "yes", "relevance": "partial"}) + "\n",
            encoding="utf-8",
        )
        labels = load_labels(path)
        assert labels[0].faithfulness is YES
        assert labels[0].relevance is PARTIAL

    def test_unlabelled_rows_are_skipped_so_a_half_done_file_still_works(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "labels.jsonl"
        path.write_text(
            json.dumps({"case_id": "q1", "faithfulness": "yes", "relevance": "yes"})
            + "\n"
            + json.dumps({"case_id": "q2", "faithfulness": "", "relevance": ""})
            + "\n",
            encoding="utf-8",
        )
        assert [label.case_id for label in load_labels(path)] == ["q1"]

    def test_an_unrecognised_label_is_refused_with_its_line(self, tmp_path: Path) -> None:
        # Silently treating it as "no" would put somebody's unfinished work into
        # a published number.
        path = tmp_path / "labels.jsonl"
        path.write_text(
            json.dumps({"case_id": "q1", "faithfulness": "probably", "relevance": "yes"}) + "\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="use one of"):
            load_labels(path)

    def test_a_person_may_not_record_undecided(self, tmp_path: Path) -> None:
        # A blank row is skipped; an "undecided" one would be counted. A person
        # who cannot decide should leave it out.
        path = tmp_path / "labels.jsonl"
        path.write_text(
            json.dumps({"case_id": "q1", "faithfulness": "undecided", "relevance": "yes"}) + "\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="leave the case blank"):
            load_labels(path)

    def test_malformed_json_names_the_line(self, tmp_path: Path) -> None:
        path = tmp_path / "labels.jsonl"
        path.write_text("{not json\n", encoding="utf-8")
        with pytest.raises(ValueError, match=":1"):
            load_labels(path)
