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
from dataclasses import dataclass, field
from typing import Protocol

from paimon.application.use_cases import Answer
from paimon.evaluation.attribution import AttributionReport, check_answer
from paimon.evaluation.dataset import EvaluationDataset
from paimon.evaluation.statistics import (
    Estimate,
    PairedDifference,
    clustered_estimate,
    compare_metric,
    group_by_document,
)


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
class AnsweringReport:
    """Everything one answering run produced."""

    dataset: str
    configuration: str
    metrics: AnsweringMetrics
    cases: tuple[AnswerCaseReport, ...]
    scores: Mapping[str, tuple[float, ...]] = field(default_factory=dict)
    clusters: tuple[str, ...] = ()

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


async def run_answering_benchmark(
    dataset: EvaluationDataset,
    answerer: Answerer,
    documents: Mapping[str, str],
    *,
    tenant_id: str,
    configuration: str = "unnamed",
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
    "AnswerCaseReport",
    "Answerer",
    "AnsweringMetrics",
    "AnsweringReport",
    "run_answering_benchmark",
]
