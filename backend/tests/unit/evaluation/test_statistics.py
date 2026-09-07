"""The statistics, checked against values that can be looked up.

A numerical routine that agrees with itself is not evidence of anything. The
tail probabilities and critical values here come from published t-tables, so a
regression in the continued fraction fails as a wrong number rather than as a
plausible one — which is how the first version of it shipped negative
probabilities before these tests existed.
"""

import pytest

from paimon.evaluation.statistics import (
    Estimate,
    clustered_estimate,
    correlation,
    critical_t,
    estimate,
    group_by_document,
    paired_difference,
    student_t_sf,
    to_mapping,
)


class TestTheTDistribution:
    @pytest.mark.parametrize(
        ("t", "degrees_of_freedom", "expected"),
        [
            (12.706, 1, 0.05),
            (4.604, 4, 0.01),
            (2.262, 9, 0.05),
            (2.145, 14, 0.05),
            (2.042, 30, 0.05),
        ],
    )
    def test_the_two_sided_tail_matches_the_table(
        self, t: float, degrees_of_freedom: int, expected: float
    ) -> None:
        assert 2 * student_t_sf(t, degrees_of_freedom) == pytest.approx(expected, abs=5e-4)

    @pytest.mark.parametrize(
        ("confidence", "degrees_of_freedom", "expected"),
        [
            (0.95, 1, 12.706),
            (0.95, 9, 2.262),
            (0.95, 14, 2.145),
            (0.99, 14, 2.977),
            (0.95, 30, 2.042),
        ],
    )
    def test_the_critical_value_matches_the_table(
        self, confidence: float, degrees_of_freedom: int, expected: float
    ) -> None:
        assert critical_t(confidence, degrees_of_freedom) == pytest.approx(expected, abs=1e-3)

    def test_zero_is_the_middle(self) -> None:
        assert student_t_sf(0.0, 10) == pytest.approx(0.5)

    def test_the_tails_are_symmetric(self) -> None:
        assert student_t_sf(-1.5, 10) == pytest.approx(1.0 - student_t_sf(1.5, 10))

    def test_more_degrees_of_freedom_narrow_the_interval(self) -> None:
        # The whole reason the number of clusters matters rather than the number
        # of questions.
        assert critical_t(0.95, 3) > critical_t(0.95, 30)


class TestEstimates:
    def test_the_mean_is_the_mean(self) -> None:
        assert estimate([0.0, 1.0, 1.0, 0.0]).mean == pytest.approx(0.5)

    def test_the_standard_error_shrinks_with_more_observations(self) -> None:
        few = estimate([0.0, 1.0] * 5)
        many = estimate([0.0, 1.0] * 50)
        assert many.standard_error < few.standard_error

    def test_a_constant_sample_has_no_spread(self) -> None:
        measured = estimate([1.0, 1.0, 1.0])
        assert measured.standard_error == 0.0
        assert measured.interval() == (1.0, 1.0)

    def test_one_observation_reports_no_spread_rather_than_guessing(self) -> None:
        # Honest, and the reason one question is not a benchmark.
        assert estimate([0.7]).standard_error == 0.0

    def test_an_empty_sample_is_zero_with_nothing_behind_it(self) -> None:
        measured = estimate([])
        assert measured.n == 0
        assert measured.mean == 0.0

    def test_the_interval_is_centred_on_the_mean(self) -> None:
        low, high = estimate([0.0, 1.0, 1.0, 0.0, 1.0]).interval()
        assert (low + high) / 2 == pytest.approx(0.6)

    def test_a_higher_confidence_gives_a_wider_interval(self) -> None:
        measured = estimate([0.0, 1.0, 1.0, 0.0, 1.0])
        narrow = measured.interval(0.90)
        wide = measured.interval(0.99)
        assert wide[0] < narrow[0]
        assert wide[1] > narrow[1]

    def test_it_formats_as_a_measurement_not_a_number(self) -> None:
        assert (
            "±" in estimate([0.0, 1.0, 1.0]).format() or "+/-" in estimate([0.0, 1.0, 1.0]).format()
        )


class TestClustering:
    def test_correlated_questions_widen_the_interval(self) -> None:
        # The point of the whole exercise. Eight questions from two documents,
        # each document answered consistently: treating them as eight
        # independent observations claims a precision the data does not have.
        values = [1.0, 1.0, 1.0, 1.0, 0.0, 0.0, 0.0, 0.0]
        documents = ["a", "a", "a", "a", "b", "b", "b", "b"]
        naive = estimate(values)
        clustered = clustered_estimate(values, documents)
        assert clustered.standard_error > naive.standard_error

    def test_the_mean_is_unchanged_by_clustering(self) -> None:
        values = [1.0, 1.0, 0.0, 0.0]
        assert clustered_estimate(values, ["a", "a", "b", "b"]).mean == pytest.approx(
            estimate(values).mean
        )

    def test_degrees_of_freedom_count_groups_not_observations(self) -> None:
        # Eight questions from two documents carry about as much information as
        # two independent observations, and the interval has to say so.
        clustered = clustered_estimate([1.0] * 4 + [0.0] * 4, ["a"] * 4 + ["b"] * 4)
        assert clustered.degrees_of_freedom == 1

    def test_one_cluster_reports_no_spread(self) -> None:
        # One cluster is one observation. A spread from it would be a claim to
        # have measured something that was never varied.
        assert clustered_estimate([1.0, 0.0, 1.0], ["a", "a", "a"]).standard_error == 0.0

    def test_mismatched_lengths_are_refused(self) -> None:
        with pytest.raises(ValueError, match="cluster labels"):
            clustered_estimate([1.0, 0.0], ["a"])

    def test_questions_are_grouped_by_the_documents_they_draw_on(self) -> None:
        assert group_by_document([["a"], ["a"], ["b", "a"], []]) == [
            "a",
            "a",
            "a+b",
            "unattributed",
        ]


class TestPairedComparison:
    def test_pairing_beats_comparing_aggregates(self) -> None:
        # The reason the whole batch exists. Two configurations that agree about
        # which questions are hard, one of them a little better everywhere: the
        # aggregates overlap, the paired difference does not.
        hard_and_easy = [0.1, 0.9, 0.2, 0.8, 0.15, 0.85, 0.25, 0.75, 0.3, 0.7]
        better = [value + 0.05 for value in hard_and_easy]

        paired = paired_difference(better, hard_and_easy)
        assert paired.is_significant()

        # Unpaired, the spread of question difficulty swamps a 0.05 difference.
        a, b = estimate(better), estimate(hard_and_easy)
        assert a.interval()[0] < b.interval()[1]

    def test_the_difference_is_this_minus_that(self) -> None:
        assert paired_difference([1.0, 1.0], [0.0, 0.0]).mean == pytest.approx(1.0)
        assert paired_difference([0.0, 0.0], [1.0, 1.0]).mean == pytest.approx(-1.0)

    def test_no_difference_is_not_significant(self) -> None:
        scores = [0.1, 0.9, 0.4, 0.6, 0.5]
        assert not paired_difference(scores, scores).is_significant()

    def test_it_reports_how_much_pairing_bought(self) -> None:
        # The correlation is what pairing exploits, so it is reported rather than
        # left implicit: near zero and pairing bought nothing.
        scores = [0.1, 0.9, 0.4, 0.6, 0.5]
        assert paired_difference([s + 0.1 for s in scores], scores).correlation == pytest.approx(
            1.0
        )

    def test_comparing_different_numbers_of_questions_is_refused(self) -> None:
        # Pairing scores from different questions is not a paired comparison; it
        # is a wrong number with a confident interval around it.
        with pytest.raises(ValueError, match="same questions"):
            paired_difference([1.0, 0.0], [1.0])

    def test_clusters_widen_the_difference_interval_too(self) -> None:
        a = [1.0, 1.0, 1.0, 1.0, 0.0, 0.0, 0.0, 0.0]
        b = [0.0] * 8
        documents = ["x"] * 4 + ["y"] * 4
        assert (
            paired_difference(a, b, documents).standard_error
            > paired_difference(a, b).standard_error
        )

    def test_a_difference_with_no_spread_is_certain_not_insignificant(self) -> None:
        # Every question improved by exactly one point. That is as significant as
        # a result gets, and the obvious guard against dividing by a zero
        # standard error reported it as noise until this test said otherwise.
        difference = paired_difference([1.0] * 8, [0.0] * 8)
        assert difference.interval()[0] > 0.0
        assert difference.p_value == 0.0
        assert difference.is_significant()

    def test_no_difference_and_no_spread_is_not_significant(self) -> None:
        # The other half of the same edge, and the reason it cannot just return
        # infinity: identical scores are identical, not certainly different.
        difference = paired_difference([1.0] * 8, [1.0] * 8)
        assert difference.p_value == pytest.approx(1.0)
        assert not difference.is_significant()


class TestCorrelation:
    def test_identical_scores_correlate_perfectly(self) -> None:
        assert correlation([0.1, 0.5, 0.9], [0.1, 0.5, 0.9]) == pytest.approx(1.0)

    def test_opposite_scores_correlate_negatively(self) -> None:
        assert correlation([0.1, 0.5, 0.9], [0.9, 0.5, 0.1]) == pytest.approx(-1.0)

    def test_a_constant_correlates_with_nothing(self) -> None:
        # Zero rather than a division by zero: a configuration that scored the
        # same everywhere agreed with nothing in particular.
        assert correlation([0.5, 0.5, 0.5], [0.1, 0.5, 0.9]) == 0.0


class TestReporting:
    def test_an_estimate_serialises_with_its_interval(self) -> None:
        # So a report on disk carries the uncertainty too. A JSON file with bare
        # means is the same mistake one file further along.
        rendered = to_mapping(Estimate(mean=0.5, standard_error=0.1, n=10, clusters=4))
        assert rendered["mean"] == 0.5
        assert rendered["clusters"] == 4
        assert rendered["ci_low"] is not None
        assert rendered["ci_high"] is not None
