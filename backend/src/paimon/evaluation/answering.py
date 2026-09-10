"""Benchmarking the answers, not only the retrieval.

Retrieval can be perfect and the answer still wrong, so this runs the answering
use case over the same golden set and measures what came out. Everything here is
**verified rather than judged**: the citations are followed into the corpus, the
markers are counted, and no model is asked for an opinion (ADR-0030).

The numbers carry intervals and the runs pair, exactly as the retrieval benchmark
does — the reasoning is in ADR-0029 and applies unchanged, because this is the
same fifteen questions about the same five documents.
"""

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Protocol

from paimon.application.use_cases import Answer
from paimon.evaluation.attribution import AttributionReport, check_answer
from paimon.evaluation.calibration import Agreement, Calibration, HumanLabel, agreement
from paimon.evaluation.dataset import EvaluationCase, EvaluationDataset
from paimon.evaluation.judging import AnswerJudge, JudgedAnswer, Verdict
from paimon.evaluation.progress import Progress
from paimon.evaluation.statistics import (
    Estimate,
    PairedDifference,
    clustered_estimate,
    compare_metric,
    estimate,
    group_by_document,
)


@dataclass(frozen=True, slots=True)
class Judging:
    """A judge and what is known about it.

    One value rather than two arguments, because the two travel together and
    separating them invites a run that used a self-judging model and forgot to
    say so — which is the one fact that changes how its numbers should be read.

    Attributes:
        judge: The model to ask, when this run asks one.
        self_judged: Whether it is the model that produced the answers.
    """

    judge: AnswerJudge | None = None
    self_judged: bool = False
    labels: Sequence[HumanLabel] = ()
    """A person's verdicts on some of the same cases. Without them the judge is
    reported as uncalibrated."""


#: A run with no judge. A module-level value rather than a default constructed
#: per call: it is immutable, so one is enough.
UNJUDGED = Judging()


class Answerer(Protocol):
    """What the benchmark needs of an answering implementation."""

    async def answer(self, question: str, *, tenant_id: str) -> Answer:
        """Answer a question from the indexed corpus."""
        ...


@dataclass(frozen=True, slots=True)
class AnswerCaseReport:
    """One question, its answer, and whether the answer holds up."""

    case_id: str
    question: str
    text: str
    grounded: bool
    attribution: AttributionReport
    latency_ms: float
    total_tokens: int
    sources: tuple[str, ...] = ()
    """The numbered passages this answer was generated from, in marker order.

    Recorded on the report, not only used and dropped: they are the evidence the
    faithfulness verdict rests on, and a judged number whose evidence was thrown
    away cannot be checked by a person afterwards — which is the whole of
    calibration."""

    judged: JudgedAnswer | None = None


@dataclass(frozen=True, slots=True)
class AnsweringMetrics:
    """Aggregate answer quality, each with its uncertainty.

    Attributes:
        cases: Questions answered.
        grounded_rate: Answers that cited anything at all. An ungrounded answer
            is not a failure — "the sources do not cover this" is the right
            answer to some questions — which is why it is reported beside the
            others rather than folded into them.
        citation_accuracy: Of the citations made, how many survived being
            followed into the source.
        cited_sentence_rate: Of the sentences written, how many carried a marker.
        fully_attributed_rate: Answers where every citation resolved **and**
            every sentence carried one. The strict reading, and the one that
            corresponds to what the platform promises.
    """

    cases: int
    grounded_rate: Estimate
    citation_accuracy: Estimate
    cited_sentence_rate: Estimate
    fully_attributed_rate: Estimate


@dataclass(frozen=True, slots=True)
class JudgedMetrics:
    """What a model thought, kept apart from what was verified.

    A separate type rather than more fields on :class:`AnsweringMetrics`, and the
    separation is the design: these numbers come from a model's opinion of
    another model's output, and on the closest measured comparison such a judge
    agreed with human assessors 56% of the time while erring towards generosity
    (ADR-0030). They are worth having and they are not the same kind of number.

    Attributes:
        judge_model: Which model produced them. Two runs judged by different
            models are not comparable.
        samples: How many times each question was put to the judge.
        judged: Questions where the judge reached a verdict.
        undecided: Questions where it did not, or its reply could not be read.
            Excluded from the means rather than counted as failures — "the judge
            broke" and "the answer was bad" are different facts — and reported,
            because a judge abstaining on half a dataset invalidates the rest.
        disagreements: Questions where repeated samples did not agree. Zero when
            only one sample was taken, which measures nothing.
        faithfulness: Whether answers stayed inside the sources they were shown.
            The precision half — did it invent anything.
        completeness: Whether they carried what the golden passages say. The
            recall half — did it leave anything out. Reported beside
            faithfulness and never averaged with it: an answer can invent
            nothing and say nothing, and one number cannot tell that apart from
            a good one.
        relevance: Whether they addressed the question asked.
        self_judged: Whether the judge is the model that produced the answers.
            Recorded so a flattering number cannot be quoted without it.
    """

    judge_model: str
    samples: int
    judged: int
    undecided: int
    disagreements: int
    faithfulness: Estimate
    completeness: Estimate
    relevance: Estimate
    self_judged: bool = False
    calibration: Calibration | None = None
    """How well this judge agreed with a person, when somebody labelled a sample.
    None means uncalibrated — the numbers above are a figure rather than a
    measurement, and the report says so."""


@dataclass(frozen=True, slots=True)
class AnsweringReport:
    """Everything one answering run produced."""

    dataset: str
    configuration: str
    metrics: AnsweringMetrics
    cases: tuple[AnswerCaseReport, ...]
    scores: Mapping[str, tuple[float, ...]] = field(default_factory=dict)
    clusters: tuple[str, ...] = ()
    judged: JudgedMetrics | None = None
    """None when no judge was configured. A run with no judge reports no judged
    section at all, rather than a section of empty columns that reads like a
    measurement."""

    @property
    def unverifiable(self) -> tuple[AnswerCaseReport, ...]:
        """Answers carrying a citation that does not survive being followed.

        The list worth reading, and the one that should be empty. A citation that
        does not resolve is the platform doing the exact thing it exists not to
        do — sounding right about something a reader cannot check.
        """
        return tuple(
            case for case in self.cases if any(not check.ok for check in case.attribution.checks)
        )

    def compare(
        self, other: "AnsweringReport", metric: str = "fully_attributed_rate"
    ) -> PairedDifference:
        """Compare this run against another, question by question.

        Raises:
            ValueError: If the runs are not comparable, or the metric is unknown.
        """
        if self.dataset != other.dataset:
            msg = f"different datasets: '{self.dataset}' and '{other.dataset}'"
            raise ValueError(msg)
        return compare_metric(self.scores, other.scores, metric, self.clusters or None)


async def run_answering_benchmark(  # noqa: PLR0913  collaborators, not flags
    dataset: EvaluationDataset,
    answerer: Answerer,
    documents: Mapping[str, str],
    *,
    tenant_id: str,
    configuration: str = "unnamed",
    judging: Judging = UNJUDGED,
    progress: Progress | None = None,
) -> AnsweringReport:
    """Answer every question in a dataset and verify what came back.

    Args:
        dataset: The golden set.
        answerer: What to measure.
        documents: The corpus as it was indexed, by document id. Supplied by the
            caller rather than read from disk: the offsets in a citation are into
            the **normalized** text a parser produced, and checking them against
            the raw file would fail for every document the parser touched.
        tenant_id: Tenant the corpus was ingested under.
        configuration: A label for what was measured.
        judging: The judge to ask, if any, and whether it is the model that
            produced the answers. Its verdicts are reported in their own
            section, never mixed with the verified numbers.
        progress: Notified after each case, when the caller wants to watch. A
            case here is a generation and up to three judgements, so this is the
            slowest loop in the project.

    Returns:
        The report, including the answers whose citations did not survive.
    """
    cases: list[AnswerCaseReport] = []

    for case in dataset:
        started = time.perf_counter()
        answer = await answerer.answer(case.question, tenant_id=tenant_id)
        latency_ms = round((time.perf_counter() - started) * 1000, 3)

        report = AnswerCaseReport(
            case_id=case.case_id,
            question=case.question,
            text=answer.text,
            grounded=answer.grounded,
            attribution=check_answer(answer.text, answer.citations, documents),
            latency_ms=latency_ms,
            total_tokens=answer.usage.total_tokens if answer.usage else 0,
            sources=answer.sources,
        )
        # Judged here rather than in a second pass over the reports, because
        # faithfulness is graded against the sources this answer was generated
        # from and a report does not carry them. The second pass is what made it
        # convenient to reach for the golden passages instead (ADR-0033).
        if judging.judge is not None:
            report = replace(report, judged=await _judge(judging.judge, case, answer))
        cases.append(report)
        if progress is not None:
            progress(done=len(cases), total=len(dataset), case_id=case.case_id)

    clusters = group_by_document(
        [[passage.document_id for passage in case.supporting] for case in dataset]
    )
    scores = _scores(cases)
    return AnsweringReport(
        dataset=dataset.name,
        configuration=configuration,
        metrics=_summarize(cases, scores, clusters),
        cases=tuple(cases),
        scores={name: tuple(values) for name, values in scores.items()},
        clusters=tuple(clusters),
        judged=(
            _summarize_judged(cases, judging.judge, judging) if judging.judge is not None else None
        ),
    )


async def _judge(judge: AnswerJudge, case: EvaluationCase, answer: Answer) -> JudgedAnswer:
    """Put one answer to the judge, each rubric against its own evidence.

    The three anchors are deliberately different, and this function is where the
    difference is enforced:

    * **Faithfulness** against ``answer.sources`` — the numbered passages the
      model was shown, in the order its markers refer to.
    * **Completeness** against the golden passages, which is the one question
      they were written to answer.
    * **Relevance** against neither. Handing the judge the expected passages
      here would let it reward an answer for matching them rather than for
      answering the question.
    """
    references = [passage.quote for passage in case.supporting]
    return JudgedAnswer(
        case_id=case.case_id,
        faithfulness=await judge.judge_faithfulness(case.question, answer.text, answer.sources),
        completeness=await judge.judge_completeness(case.question, answer.text, references),
        relevance=await judge.judge_relevance(case.question, answer.text),
    )


def _summarize_judged(
    cases: Sequence[AnswerCaseReport], judge: AnswerJudge, judging: "Judging"
) -> JudgedMetrics:
    """Aggregate the verdicts, excluding the ones the judge could not reach.

    Unclustered, and deliberately: the clustering in ADR-0029 models questions
    about one document sharing a difficulty. A judge's errors cluster by *rubric
    and phrasing*, which is a structure this dataset cannot estimate, and
    borrowing the document clustering would put a confident-looking interval on
    the wrong correlation. The interval here is the plain one, and the sample
    counts are reported beside it.
    """
    verdicts = [case.judged for case in cases if case.judged is not None]

    def scored(rubric: str) -> list[float]:
        """Every verdict for one rubric, abstentions left out."""
        return [
            getattr(judgement, rubric).verdict.score
            for judgement in verdicts
            if getattr(judgement, rubric).verdict is not Verdict.UNDECIDED
        ]

    rubrics = ("faithfulness", "completeness", "relevance")
    undecided = sum(
        1
        for judgement in verdicts
        if any(getattr(judgement, rubric).verdict is Verdict.UNDECIDED for rubric in rubrics)
    )
    return JudgedMetrics(
        judge_model=judge.model_id,
        samples=max((judgement.faithfulness.samples for judgement in verdicts), default=1),
        judged=len(verdicts) - undecided,
        undecided=undecided,
        disagreements=sum(
            1
            for judgement in verdicts
            if not all(getattr(judgement, rubric).unanimous for rubric in rubrics)
        ),
        faithfulness=estimate(scored("faithfulness")),
        completeness=estimate(scored("completeness")),
        relevance=estimate(scored("relevance")),
        self_judged=judging.self_judged,
        calibration=_calibrate(cases, judge, judging.labels),
    )


def _calibrate(
    cases: Sequence[AnswerCaseReport], judge: AnswerJudge, labels: Sequence[HumanLabel]
) -> Calibration | None:
    """Compare the judge's verdicts against a person's, where both exist."""
    if not labels:
        return None
    judged = {case.case_id: case.judged for case in cases if case.judged is not None}
    human = {label.case_id: label for label in labels}

    def compare(rubric: str) -> Agreement:
        """One rubric's agreement, over the cases a person actually labelled."""
        theirs = {case_id: getattr(value, rubric).verdict for case_id, value in judged.items()}
        ours = {
            case_id: verdict
            for case_id, label in human.items()
            if (verdict := getattr(label, rubric)) is not None
        }
        return agreement(theirs, ours)

    return Calibration(
        judge_model=judge.model_id,
        labels=len(labels),
        faithfulness=compare("faithfulness"),
        completeness=compare("completeness"),
        relevance=compare("relevance"),
    )


def _scores(cases: Sequence[AnswerCaseReport]) -> dict[str, list[float]]:
    """One score per case per metric, which is what a paired comparison needs."""
    return {
        "grounded_rate": [1.0 if case.grounded else 0.0 for case in cases],
        "citation_accuracy": [case.attribution.citation_accuracy for case in cases],
        "cited_sentence_rate": [case.attribution.cited_sentence_rate for case in cases],
        "fully_attributed_rate": [
            1.0 if case.attribution.is_fully_attributed else 0.0 for case in cases
        ],
    }


def _summarize(
    cases: Sequence[AnswerCaseReport],
    scores: Mapping[str, Sequence[float]],
    clusters: Sequence[str],
) -> AnsweringMetrics:
    """Aggregate, clustered by document as the retrieval metrics are."""
    empty = Estimate(mean=0.0, standard_error=0.0, n=0)
    if not cases:
        return AnsweringMetrics(
            cases=0,
            grounded_rate=empty,
            citation_accuracy=empty,
            cited_sentence_rate=empty,
            fully_attributed_rate=empty,
        )
    return AnsweringMetrics(
        cases=len(cases),
        grounded_rate=clustered_estimate(scores["grounded_rate"], clusters),
        citation_accuracy=clustered_estimate(scores["citation_accuracy"], clusters),
        cited_sentence_rate=clustered_estimate(scores["cited_sentence_rate"], clusters),
        fully_attributed_rate=clustered_estimate(scores["fully_attributed_rate"], clusters),
    )


__all__ = [
    "UNJUDGED",
    "AnswerCaseReport",
    "Answerer",
    "AnsweringMetrics",
    "AnsweringReport",
    "JudgedMetrics",
    "Judging",
    "run_answering_benchmark",
]
