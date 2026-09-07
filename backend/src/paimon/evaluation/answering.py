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
from paimon.evaluation.dataset import EvaluationDataset
from paimon.evaluation.judging import AnswerJudge, JudgedAnswer, Verdict
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
        faithfulness: Whether answers stayed within what their sources support.
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
    relevance: Estimate
    self_judged: bool = False


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
        judging: The judge to ask about faithfulness and relevance, if any, and
            whether it is the model that produced the answers. Its verdicts are
            reported in their own section, never mixed with the verified numbers.

    Returns:
        The report, including the answers whose citations did not survive.
    """
    cases: list[AnswerCaseReport] = []

    for case in dataset:
        started = time.perf_counter()
        answer = await answerer.answer(case.question, tenant_id=tenant_id)
        latency_ms = round((time.perf_counter() - started) * 1000, 3)

        cases.append(
            AnswerCaseReport(
                case_id=case.case_id,
                question=case.question,
                text=answer.text,
                grounded=answer.grounded,
                attribution=check_answer(answer.text, answer.citations, documents),
                latency_ms=latency_ms,
                total_tokens=answer.usage.total_tokens if answer.usage else 0,
            )
        )

    if judging.judge is not None:
        cases = await _judge_all(cases, dataset, judging.judge)

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
            _summarize_judged(cases, judging.judge, self_judged=judging.self_judged)
            if judging.judge is not None
            else None
        ),
    )


async def _judge_all(
    cases: Sequence[AnswerCaseReport], dataset: EvaluationDataset, judge: AnswerJudge
) -> list[AnswerCaseReport]:
    """Put every answer to the judge, against the passages the golden set names."""
    by_id = {case.case_id: case for case in dataset}
    judged: list[AnswerCaseReport] = []
    for case in cases:
        expected = by_id[case.case_id]
        references = [passage.quote for passage in expected.supporting]
        judged.append(
            replace(
                case,
                judged=JudgedAnswer(
                    case_id=case.case_id,
                    faithfulness=await judge.judge_faithfulness(
                        case.question, case.text, references
                    ),
                    relevance=await judge.judge_relevance(case.question, case.text),
                ),
            )
        )
    return judged


def _summarize_judged(
    cases: Sequence[AnswerCaseReport], judge: AnswerJudge, *, self_judged: bool
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
    faithful = [
        judgement.faithfulness.verdict.score
        for judgement in verdicts
        if judgement.faithfulness.verdict is not Verdict.UNDECIDED
    ]
    relevant = [
        judgement.relevance.verdict.score
        for judgement in verdicts
        if judgement.relevance.verdict is not Verdict.UNDECIDED
    ]
    undecided = sum(
        1
        for judgement in verdicts
        if Verdict.UNDECIDED in (judgement.faithfulness.verdict, judgement.relevance.verdict)
    )
    return JudgedMetrics(
        judge_model=judge.model_id,
        samples=max((judgement.faithfulness.samples for judgement in verdicts), default=1),
        judged=len(verdicts) - undecided,
        undecided=undecided,
        disagreements=sum(
            1
            for judgement in verdicts
            if not (judgement.faithfulness.unanimous and judgement.relevance.unanimous)
        ),
        faithfulness=estimate(faithful),
        relevance=estimate(relevant),
        self_judged=self_judged,
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
