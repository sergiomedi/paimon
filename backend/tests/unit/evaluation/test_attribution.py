"""Following citations into the corpus.

The cases here are the ways an answer can be unfaithful while looking correct: a
quote that is not where it says it is, offsets past the end of the document, a
document that does not exist, a sentence asserting something with no marker at
all. Every one of them is caught by arithmetic rather than by asking a model —
which matters, because on precisely this question a frontier model and human
assessors agreed 56% of the time, and the model erred towards saying the
citation supported the claim (ADR-0030).
"""

import pytest

from paimon.domain.value_objects import Citation
from paimon.evaluation.attribution import Attribution, check_answer, check_citation

RUNBOOK = (
    "# Node maintenance\n\n"
    "Cordon the node first so the scheduler stops placing new pods on it. "
    "Eviction stalls indefinitely when a disruption budget cannot be satisfied.\n"
)
CORPUS = {"runbook": RUNBOOK}


def citation(
    *,
    quote: str,
    start: int,
    end: int,
    document_id: str = "runbook",
    marker: int = 1,
) -> Citation:
    return Citation(
        marker=marker,
        document_id=document_id,
        chunk_id="c-1",
        source_uri="https://example.test/runbook.md",
        title="Node maintenance",
        heading_path=("Node maintenance",),
        start_char=start,
        end_char=end,
        quote=quote,
    )


class TestFollowingOneCitation:
    def test_a_citation_that_points_at_its_quote_resolves(self) -> None:
        quote = "Cordon the node first"
        start = RUNBOOK.index(quote)
        check = check_citation(citation(quote=quote, start=start, end=start + len(quote)), CORPUS)
        assert check.attribution is Attribution.RESOLVED
        assert check.ok

    def test_a_quote_that_is_not_in_the_span_is_a_misquote(self) -> None:
        # The failure that matters: the citation looks perfectly well-formed and
        # the text it names says something else. No model is needed to see it.
        check = check_citation(
            citation(quote="Reboot the node immediately", start=0, end=40), CORPUS
        )
        assert check.attribution is Attribution.MISQUOTED
        assert not check.ok
        assert check.found  # what was actually there, for a reader to compare

    def test_offsets_past_the_end_of_the_document_are_caught(self) -> None:
        check = check_citation(citation(quote="anything", start=10_000, end=10_050), CORPUS)
        assert check.attribution is Attribution.OUT_OF_RANGE

    def test_a_citation_to_a_document_that_does_not_exist_is_caught(self) -> None:
        check = check_citation(
            citation(quote="anything", start=0, end=8, document_id="invented"), CORPUS
        )
        assert check.attribution is Attribution.UNKNOWN_DOCUMENT

    def test_whitespace_differences_do_not_break_a_citation(self) -> None:
        # A chunker that normalised a line break has not invented a citation, and
        # the golden set matches the same way (ADR-0013).
        quote = "Cordon the node   first\n so the scheduler"
        start = RUNBOOK.index("Cordon the node first so the scheduler")
        check = check_citation(citation(quote=quote, start=start, end=start + 40), CORPUS)
        assert check.attribution is Attribution.RESOLVED


class TestCheckingAWholeAnswer:
    def test_a_fully_attributed_answer_is_recognised(self) -> None:
        quote = "Cordon the node first"
        start = RUNBOOK.index(quote)
        report = check_answer(
            "Cordon the node first [1].",
            [citation(quote=quote, start=start, end=start + len(quote))],
            CORPUS,
        )
        assert report.is_fully_attributed
        assert report.citation_accuracy == 1.0
        assert report.cited_sentence_rate == 1.0

    def test_a_sentence_without_a_marker_is_found(self) -> None:
        # The claim a reader cannot check, which is the thing this platform
        # exists not to produce.
        quote = "Cordon the node first"
        start = RUNBOOK.index(quote)
        report = check_answer(
            "Cordon the node first [1]. Then reboot it.",
            [citation(quote=quote, start=start, end=start + len(quote))],
            CORPUS,
        )
        assert report.uncited_sentences == ("Then reboot it.",)
        assert not report.is_fully_attributed
        assert report.cited_sentence_rate == pytest.approx(0.5)

    def test_an_answer_citing_nothing_scores_zero_not_one(self) -> None:
        # Vacuous truth reads better and says an answer citing nothing is
        # perfectly attributed, which is exactly backwards.
        report = check_answer("The runbook says to drain it.", [], CORPUS)
        assert report.citation_accuracy == 0.0
        assert not report.is_fully_attributed

    def test_a_marker_no_citation_explains_is_reported(self) -> None:
        # The platform strips these before answering, so a non-empty list here is
        # a regression in that stripping rather than a model quirk.
        quote = "Cordon the node first"
        start = RUNBOOK.index(quote)
        report = check_answer(
            "Cordon the node first [1]. Evictions stall [7].",
            [citation(quote=quote, start=start, end=start + len(quote))],
            CORPUS,
        )
        assert report.invented_markers == (7,)

    def test_accuracy_counts_the_citations_that_survived(self) -> None:
        good = "Cordon the node first"
        start = RUNBOOK.index(good)
        report = check_answer(
            "Cordon the node first [1]. It reboots automatically [2].",
            [
                citation(quote=good, start=start, end=start + len(good), marker=1),
                citation(quote="It reboots automatically", start=0, end=30, marker=2),
            ],
            CORPUS,
        )
        assert report.resolved == 1
        assert report.citation_accuracy == pytest.approx(0.5)

    def test_an_empty_answer_reports_nothing_rather_than_dividing_by_zero(self) -> None:
        report = check_answer("", [], CORPUS)
        assert report.sentences == 0
        assert report.cited_sentence_rate == 0.0
