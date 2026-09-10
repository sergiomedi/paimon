"""The answering benchmark, over a scripted answerer.

What is under test is the run: that every question is asked, that citations are
verified against the corpus rather than trusted, and that the aggregates carry
their intervals and pair like the retrieval ones do.
"""

import pytest

from paimon.application.use_cases import Answer, Usage
from paimon.domain.value_objects import Citation
from paimon.evaluation import (
    EvaluationCase,
    EvaluationDataset,
    HumanLabel,
    Judgement,
    Judging,
    SupportingPassage,
    Verdict,
    run_answering_benchmark,
)
from tests.fakes import ScriptedJudge

TENANT = "benchmark"
RUNBOOK = "Cordon the node first so the scheduler stops placing new pods on it."
POSTMORTEM = "The pool was exhausted by a retry storm."
CORPUS = {"runbook": RUNBOOK, "postmortem": POSTMORTEM}


def dataset(name: str = "answers-v1") -> EvaluationDataset:
    return EvaluationDataset(
        name=name,
        cases=(
            EvaluationCase(
                case_id="q1",
                question="how do I drain a node?",
                supporting=(SupportingPassage(document_id="runbook", quote="Cordon the node"),),
            ),
            EvaluationCase(
                case_id="q2",
                question="what caused INC-2451?",
                supporting=(
                    SupportingPassage(document_id="postmortem", quote="pool was exhausted"),
                ),
            ),
        ),
    )


def citation(document_id: str, quote: str, *, marker: int = 1) -> Citation:
    text = CORPUS[document_id]
    start = text.index(quote)
    return Citation(
        marker=marker,
        document_id=document_id,
        chunk_id=f"{document_id}:0",
        source_uri=f"https://example.test/{document_id}.md",
        title=document_id,
        heading_path=(),
        start_char=start,
        end_char=start + len(quote),
        quote=quote,
    )


class ScriptedAnswerer:
    """Returns a prepared answer per question, and records what it was asked."""

    def __init__(self, answers: dict[str, Answer]) -> None:
        self._answers = answers
        self.questions: list[str] = []
        self.tenants: list[str] = []

    async def answer(self, question: str, *, tenant_id: str) -> Answer:
        self.questions.append(question)
        self.tenants.append(tenant_id)
        return self._answers.get(
            question,
            Answer(
                text="",
                citations=(),
                grounded=False,
                strategy="fused",
                retrieved=0,
            ),
        )


def grounded(text: str, citations: tuple[Citation, ...]) -> Answer:
    """An answer whose sources are the whole documents it cited.

    Wider than the golden set's one-sentence quote, on purpose: that gap is
    where a correct elaboration lives, and a fixture that made the two identical
    could not tell the two rubrics apart.
    """
    return Answer(
        text=text,
        citations=citations,
        grounded=True,
        strategy="fused",
        retrieved=3,
        sources=tuple(CORPUS[citation.document_id] for citation in citations),
        usage=Usage(input_tokens=50, output_tokens=10, model_id="fake-chat-v1"),
    )


def answerer(*, faithful: bool = True) -> ScriptedAnswerer:
    """Two answers: the second is a misquote when ``faithful`` is false."""
    honest = citation("runbook", "Cordon the node first")
    second = (
        citation("postmortem", "pool was exhausted")
        if faithful
        # Points at real text that says something else entirely.
        else Citation(
            marker=1,
            document_id="postmortem",
            chunk_id="postmortem:0",
            source_uri="https://example.test/postmortem.md",
            title="postmortem",
            heading_path=(),
            start_char=0,
            end_char=20,
            quote="A disk filled up",
        )
    )
    return ScriptedAnswerer(
        {
            "how do I drain a node?": grounded("Cordon the node first [1].", (honest,)),
            "what caused INC-2451?": grounded("The pool was exhausted [1].", (second,)),
        }
    )


class TestRunning:
    async def test_it_asks_every_question_as_the_given_tenant(self) -> None:
        scripted = answerer()
        await run_answering_benchmark(dataset(), scripted, CORPUS, tenant_id=TENANT)
        assert scripted.questions == ["how do I drain a node?", "what caused INC-2451?"]
        assert set(scripted.tenants) == {TENANT}

    async def test_a_faithful_run_is_fully_attributed(self) -> None:
        report = await run_answering_benchmark(
            dataset(), answerer(), CORPUS, tenant_id=TENANT, configuration="honest"
        )
        assert report.metrics.fully_attributed_rate.mean == 1.0
        assert report.metrics.citation_accuracy.mean == 1.0
        assert report.unverifiable == ()

    async def test_a_misquote_is_caught_and_named(self) -> None:
        # The point of the batch. The answer reads correctly, the citation is
        # well-formed, and the text it points at says something else.
        report = await run_answering_benchmark(
            dataset(), answerer(faithful=False), CORPUS, tenant_id=TENANT
        )
        assert report.metrics.citation_accuracy.mean == pytest.approx(0.5)
        assert [case.case_id for case in report.unverifiable] == ["q2"]

    async def test_an_ungrounded_answer_is_reported_not_punished(self) -> None:
        # "The sources do not cover this" is the right answer to some questions,
        # so it is counted separately rather than folded into the others.
        report = await run_answering_benchmark(
            dataset(), ScriptedAnswerer({}), CORPUS, tenant_id=TENANT
        )
        assert report.metrics.grounded_rate.mean == 0.0
        assert report.metrics.cases == 2

    async def test_what_the_answers_cost_is_recorded(self) -> None:
        report = await run_answering_benchmark(dataset(), answerer(), CORPUS, tenant_id=TENANT)
        assert all(case.total_tokens == 60 for case in report.cases)


class TestUncertaintyAndPairing:
    async def test_the_aggregates_carry_intervals_clustered_by_document(self) -> None:
        report = await run_answering_benchmark(dataset(), answerer(), CORPUS, tenant_id=TENANT)
        assert report.metrics.citation_accuracy.clusters == 2
        assert report.metrics.citation_accuracy.n == 2

    async def test_two_runs_compare_question_by_question(self) -> None:
        honest = await run_answering_benchmark(
            dataset(), answerer(), CORPUS, tenant_id=TENANT, configuration="honest"
        )
        sloppy = await run_answering_benchmark(
            dataset(), answerer(faithful=False), CORPUS, tenant_id=TENANT, configuration="sloppy"
        )
        difference = honest.compare(sloppy, "citation_accuracy")
        assert difference.mean > 0
        assert difference.n == 2

    async def test_comparing_different_datasets_is_refused(self) -> None:
        first = await run_answering_benchmark(dataset("a"), answerer(), CORPUS, tenant_id=TENANT)
        second = await run_answering_benchmark(dataset("b"), answerer(), CORPUS, tenant_id=TENANT)
        with pytest.raises(ValueError, match="different datasets"):
            first.compare(second)

    async def test_an_unknown_metric_names_the_ones_that_exist(self) -> None:
        report = await run_answering_benchmark(dataset(), answerer(), CORPUS, tenant_id=TENANT)
        with pytest.raises(ValueError, match="citation_accuracy"):
            report.compare(report, "made_up")


class TestJudgedMetrics:
    """A model's opinion, kept visibly apart from what was verified."""

    async def test_a_run_without_a_judge_reports_no_judged_section(self) -> None:
        # Rather than a section of empty columns that reads like a measurement.
        report = await run_answering_benchmark(dataset(), answerer(), CORPUS, tenant_id=TENANT)
        assert report.judged is None

    async def test_verdicts_are_aggregated_and_attributed(self) -> None:
        scripted = ScriptedJudge(model_id="qwen-judge")
        report = await run_answering_benchmark(
            dataset(), answerer(), CORPUS, tenant_id=TENANT, judging=Judging(judge=scripted)
        )
        assert report.judged is not None
        assert report.judged.judge_model == "qwen-judge"
        assert report.judged.faithfulness.mean == 1.0
        assert report.judged.relevance.mean == 1.0

    async def test_faithfulness_is_graded_against_the_sources_the_model_saw(self) -> None:
        # The regression this whole change exists for. Grading faithfulness
        # against the golden set's one-sentence quote scores a correct
        # elaboration from the rest of the corpus as unfaithful, which is a
        # measurement of the golden set's brevity and not of the answer
        # (ADR-0033). If this ever reads ["Cordon the node"] again, the metric
        # has silently gone back to measuring the wrong thing.
        scripted = ScriptedJudge()
        await run_answering_benchmark(
            dataset(), answerer(), CORPUS, tenant_id=TENANT, judging=Judging(judge=scripted)
        )
        assert scripted.sources_seen[0] == [RUNBOOK]
        assert "Cordon the node" not in scripted.sources_seen[0]

    async def test_completeness_is_graded_against_the_passages_the_golden_set_names(self) -> None:
        # The one question the golden set is the right anchor for: it was written
        # to name what answers each question.
        scripted = ScriptedJudge()
        await run_answering_benchmark(
            dataset(), answerer(), CORPUS, tenant_id=TENANT, judging=Judging(judge=scripted)
        )
        assert scripted.references_seen[0] == ["Cordon the node"]

    async def test_relevance_is_shown_no_passages_at_all(self) -> None:
        # Handing it the expected passages would let it reward an answer for
        # matching them rather than for answering the question.
        scripted = ScriptedJudge()
        await run_answering_benchmark(
            dataset(), answerer(), CORPUS, tenant_id=TENANT, judging=Judging(judge=scripted)
        )
        assert scripted.relevance_questions == ["how do I drain a node?", "what caused INC-2451?"]

    async def test_precision_and_recall_are_reported_separately(self) -> None:
        # An answer can invent nothing by saying nothing. One number cannot tell
        # that apart from a good answer, so there are two.
        scripted = ScriptedJudge(
            faithfulness={"how do I drain a node?": Verdict.YES},
            completeness={"how do I drain a node?": Verdict.NO},
            default=Verdict.YES,
        )
        report = await run_answering_benchmark(
            dataset(), answerer(), CORPUS, tenant_id=TENANT, judging=Judging(judge=scripted)
        )
        assert report.judged is not None
        assert report.judged.faithfulness.mean == 1.0
        assert report.judged.completeness.mean == 0.5

    async def test_the_report_keeps_the_sources_a_verdict_rested_on(self) -> None:
        # A judged number whose evidence was thrown away cannot be checked by a
        # person afterwards, and checking it is the whole of calibration.
        report = await run_answering_benchmark(
            dataset(), answerer(), CORPUS, tenant_id=TENANT, judging=Judging(judge=ScriptedJudge())
        )
        assert report.cases[0].sources == (RUNBOOK,)

    async def test_an_undecided_verdict_is_excluded_not_counted_as_failure(self) -> None:
        # "The judge broke" and "the answer was bad" are different facts.
        scripted = ScriptedJudge(
            faithfulness={"how do I drain a node?": Verdict.UNDECIDED},
            default=Verdict.YES,
        )
        report = await run_answering_benchmark(
            dataset(), answerer(), CORPUS, tenant_id=TENANT, judging=Judging(judge=scripted)
        )
        assert report.judged is not None
        assert report.judged.undecided == 1
        assert report.judged.judged == 1
        # The remaining answer scored 1.0, and the abstention did not drag it to 0.5.
        assert report.judged.faithfulness.mean == 1.0

    async def test_a_partial_verdict_lands_between(self) -> None:
        scripted = ScriptedJudge(
            faithfulness={"how do I drain a node?": Verdict.PARTIAL}, default=Verdict.YES
        )
        report = await run_answering_benchmark(
            dataset(), answerer(), CORPUS, tenant_id=TENANT, judging=Judging(judge=scripted)
        )
        assert report.judged is not None
        assert report.judged.faithfulness.mean == pytest.approx(0.75)

    async def test_self_judging_is_recorded_on_the_report(self) -> None:
        # So a flattering number cannot be quoted without it.
        report = await run_answering_benchmark(
            dataset(),
            answerer(),
            CORPUS,
            tenant_id=TENANT,
            judging=Judging(judge=ScriptedJudge(), self_judged=True),
        )
        assert report.judged is not None
        assert report.judged.self_judged

    async def test_judged_numbers_do_not_touch_the_verified_ones(self) -> None:
        # The separation is the design: a harsh judge must not move a number that
        # was checked by opening a file.
        harsh = ScriptedJudge(default=Verdict.NO)
        verified = await run_answering_benchmark(dataset(), answerer(), CORPUS, tenant_id=TENANT)
        judged = await run_answering_benchmark(
            dataset(), answerer(), CORPUS, tenant_id=TENANT, judging=Judging(judge=harsh)
        )
        assert judged.metrics == verified.metrics
        assert judged.judged is not None
        assert judged.judged.faithfulness.mean == 0.0


class TestCalibration:
    """Without a person's labels, a judged number is a figure, not a measurement."""

    async def test_a_judge_without_labels_is_uncalibrated(self) -> None:
        report = await run_answering_benchmark(
            dataset(), answerer(), CORPUS, tenant_id=TENANT, judging=Judging(judge=ScriptedJudge())
        )
        assert report.judged is not None
        assert report.judged.calibration is None

    async def test_labels_produce_an_agreement(self) -> None:
        report = await run_answering_benchmark(
            dataset(),
            answerer(),
            CORPUS,
            tenant_id=TENANT,
            judging=Judging(
                judge=ScriptedJudge(model_id="llama-judge"),
                labels=[
                    HumanLabel(case_id="q1", faithfulness=Verdict.YES, relevance=Verdict.YES),
                    HumanLabel(case_id="q2", faithfulness=Verdict.YES, relevance=Verdict.YES),
                ],
            ),
        )
        assert report.judged is not None
        assert report.judged.calibration is not None
        assert report.judged.calibration.labels == 2
        assert report.judged.calibration.faithfulness.compared == 2
        assert report.judged.calibration.faithfulness.raw.mean == 1.0

    async def test_a_judge_that_disagrees_with_the_person_is_visible(self) -> None:
        report = await run_answering_benchmark(
            dataset(),
            answerer(),
            CORPUS,
            tenant_id=TENANT,
            judging=Judging(
                judge=ScriptedJudge(default=Verdict.YES),
                labels=[
                    HumanLabel(case_id="q1", faithfulness=Verdict.NO, relevance=Verdict.NO),
                    HumanLabel(case_id="q2", faithfulness=Verdict.NO, relevance=Verdict.NO),
                ],
            ),
        )
        assert report.judged is not None
        assert report.judged.calibration is not None
        assert report.judged.calibration.faithfulness.raw.mean == 0.0
        assert not report.judged.calibration.is_acceptable

    async def test_labels_for_cases_that_were_not_judged_are_ignored(self) -> None:
        report = await run_answering_benchmark(
            dataset(),
            answerer(),
            CORPUS,
            tenant_id=TENANT,
            judging=Judging(
                judge=ScriptedJudge(),
                labels=[
                    HumanLabel(case_id="q1", faithfulness=Verdict.YES, relevance=Verdict.YES),
                    HumanLabel(case_id="q99", faithfulness=Verdict.NO, relevance=Verdict.NO),
                ],
            ),
        )
        assert report.judged is not None
        assert report.judged.calibration is not None
        assert report.judged.calibration.faithfulness.compared == 1

    async def test_each_rubric_is_calibrated_separately(self) -> None:
        # A judge is routinely trustworthy at one rubric and not another — this
        # project's first calibrated run came back +1.00 on relevance and +0.45
        # on faithfulness — and one averaged figure would hide the half that
        # needs the work.
        report = await run_answering_benchmark(
            dataset(),
            answerer(),
            CORPUS,
            tenant_id=TENANT,
            judging=Judging(
                judge=ScriptedJudge(default=Verdict.YES),
                labels=[
                    HumanLabel(
                        case_id="q1",
                        faithfulness=Verdict.NO,
                        completeness=Verdict.YES,
                        relevance=Verdict.YES,
                    ),
                    HumanLabel(
                        case_id="q2",
                        faithfulness=Verdict.NO,
                        completeness=Verdict.YES,
                        relevance=Verdict.YES,
                    ),
                ],
            ),
        )
        assert report.judged is not None
        calibration = report.judged.calibration
        assert calibration is not None
        assert calibration.faithfulness.raw.mean == 0.0
        assert calibration.completeness.raw.mean == 1.0
        assert calibration.relevance.raw.mean == 1.0

    async def test_a_rubric_nobody_labelled_is_left_unmeasured(self) -> None:
        # Rather than counted as a disagreement. Somebody labelling one rubric
        # across every case is doing the thing the guidance recommends, and the
        # report should say what they measured and nothing more.
        report = await run_answering_benchmark(
            dataset(),
            answerer(),
            CORPUS,
            tenant_id=TENANT,
            judging=Judging(
                judge=ScriptedJudge(),
                labels=[HumanLabel(case_id="q1", faithfulness=Verdict.YES)],
            ),
        )
        assert report.judged is not None
        calibration = report.judged.calibration
        assert calibration is not None
        assert calibration.faithfulness.compared == 1
        assert calibration.completeness.compared == 0
        assert calibration.is_acceptable


class TestProgress:
    """Fifteen minutes of silence is indistinguishable from a hung process."""

    async def test_every_case_is_reported_as_it_finishes(self) -> None:
        seen: list[tuple[int, int, str]] = []

        def watch(*, done: int, total: int, case_id: str) -> None:
            seen.append((done, total, case_id))

        await run_answering_benchmark(
            dataset(), answerer(), CORPUS, tenant_id=TENANT, progress=watch
        )
        assert seen == [(1, 2, "q1"), (2, 2, "q2")]

    async def test_a_case_is_reported_only_once_its_judging_is_done(self) -> None:
        # The judge is most of the wall clock — three calls per case against one
        # generation — so a counter that advanced before it would run ahead of
        # the work and sit at 15/15 while the process kept going.
        order: list[str] = []

        class Narrating(ScriptedJudge):
            async def judge_relevance(self, question: str, answer: str) -> Judgement:
                order.append("judged")
                return await super().judge_relevance(question, answer)

        def watch(*, done: int, total: int, case_id: str) -> None:
            order.append("reported")

        await run_answering_benchmark(
            dataset(),
            answerer(),
            CORPUS,
            tenant_id=TENANT,
            judging=Judging(judge=Narrating()),
            progress=watch,
        )
        assert order == ["judged", "reported", "judged", "reported"]

    async def test_nothing_is_reported_when_nobody_is_watching(self) -> None:
        report = await run_answering_benchmark(dataset(), answerer(), CORPUS, tenant_id=TENANT)
        assert report.metrics.cases == 2
