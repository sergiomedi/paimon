"""Running a retrieval benchmark."""

import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol

from paimon.domain.entities import Chunk
from paimon.domain.ports import SearchFilters
from paimon.evaluation.dataset import EvaluationCase, EvaluationDataset
from paimon.evaluation.metrics import (
    CaseOutcome,
    RetrievalMetrics,
    per_case_scores,
    score_case,
    summarize,
)
from paimon.evaluation.progress import Progress
from paimon.evaluation.statistics import (
    PairedDifference,
    compare_metric,
    group_by_document,
)


class Retriever(Protocol):
    """What the benchmark needs of a retrieval implementation.

    Narrower than the retrieval use case on purpose: the benchmark should be able
    to measure anything that returns ranked chunks for a question, including a
    single retriever in isolation, without that thing having to satisfy the whole
    use case.
    """

    async def retrieve(self, question: str, filters: SearchFilters) -> list[Chunk]:
        """Return chunks for a question, best first."""
        ...


@dataclass(frozen=True, slots=True)
class CaseReport:
    """One case's outcome, with what it cost."""

    outcome: CaseOutcome
    latency_ms: float
    question: str


@dataclass(frozen=True, slots=True)
class BenchmarkReport:
    """Everything one benchmark run produced.

    Carries the configuration label alongside the numbers. A metric without the
    configuration that produced it cannot be compared with anything, which is the
    only thing a benchmark is for.

    It also keeps the **per-question scores**, which is what makes two runs
    comparable properly. Subtracting two aggregates throws away the fact that
    both configurations were asked the same questions, and that fact is most of
    the available information on a dataset this size (ADR-0029).
    """

    dataset: str
    configuration: str
    metrics: RetrievalMetrics
    cases: tuple[CaseReport, ...]
    scores: Mapping[str, tuple[float, ...]] = field(default_factory=dict)
    clusters: tuple[str, ...] = ()

    def compare(self, other: "BenchmarkReport", metric: str = "ndcg_at_k") -> PairedDifference:
        """Compare this run against another, question by question.

        Args:
            other: The run to measure against. Must be the same dataset, in the
                same order — a paired comparison of different questions is not a
                paired comparison, it is a wrong number with a confident interval
                around it.
            metric: Which of the per-question scores to compare.

        Returns:
            The difference ``this - other`` with its uncertainty.

        Raises:
            ValueError: If the runs are not comparable, or the metric is unknown.
        """
        if self.dataset != other.dataset:
            msg = f"different datasets: '{self.dataset}' and '{other.dataset}'"
            raise ValueError(msg)
        return compare_metric(self.scores, other.scores, metric, self.clusters or None)

    @property
    def median_latency_ms(self) -> float:
        """Median retrieval latency across the run."""
        if not self.cases:
            return 0.0
        ordered = sorted(case.latency_ms for case in self.cases)
        middle = len(ordered) // 2
        if len(ordered) % 2:
            return ordered[middle]
        return (ordered[middle - 1] + ordered[middle]) / 2

    @property
    def failures(self) -> tuple[CaseReport, ...]:
        """Cases where nothing relevant was retrieved.

        The list worth reading. An aggregate that moved tells you something
        changed; these tell you what.
        """
        return tuple(case for case in self.cases if not case.outcome.is_answerable)


async def run_benchmark(  # noqa: PLR0913  collaborators and a label, not flags
    dataset: EvaluationDataset,
    retriever: Retriever,
    *,
    tenant_id: str,
    cutoff: int = 8,
    configuration: str = "unnamed",
    progress: Progress | None = None,
) -> BenchmarkReport:
    """Run every case in a dataset and score the results.

    Args:
        dataset: The golden set.
        retriever: What to measure.
        tenant_id: Tenant the corpus was ingested under.
        cutoff: The k the metrics are measured at.
        configuration: A label for what was measured — the chunk size, the
            embedding model, the fusion weights. Without it the numbers are
            unattributable.
        progress: Notified after each case, when the caller wants to watch.

    Returns:
        The report, including the cases that found nothing.
    """
    reports: list[CaseReport] = []
    outcomes: list[CaseOutcome] = []
    ranks_per_case: list[list[int]] = []

    for case in dataset:
        started = time.perf_counter()
        chunks = await retriever.retrieve(case.question, SearchFilters(tenant_id=tenant_id))
        latency_ms = round((time.perf_counter() - started) * 1000, 3)

        outcome = score_case(case, chunks, cutoff)
        outcomes.append(outcome)
        ranks_per_case.append(_relevant_ranks(case, chunks, cutoff))
        reports.append(CaseReport(outcome=outcome, latency_ms=latency_ms, question=case.question))
        if progress is not None:
            progress(done=len(reports), total=len(dataset), case_id=case.case_id)

    # Questions about one document are not independent observations: they share
    # its wording and whatever the chunker made of it. Clustering by the
    # documents a question draws on is crude and is much closer to the truth
    # than treating fifteen questions as fifteen independent samples.
    clusters = group_by_document(
        [[passage.document_id for passage in case.supporting] for case in dataset]
    )
    scores = per_case_scores(outcomes, ranks_per_case)

    return BenchmarkReport(
        dataset=dataset.name,
        configuration=configuration,
        metrics=summarize(outcomes, ranks_per_case, cutoff, clusters),
        cases=tuple(reports),
        scores={name: tuple(values) for name, values in scores.items()},
        clusters=tuple(clusters),
    )


def _relevant_ranks(case: EvaluationCase, chunks: list[Chunk], cutoff: int) -> list[int]:
    """Positions at which a retrieved chunk supported something expected."""
    return [
        position
        for position, chunk in enumerate(chunks[:cutoff], start=1)
        if any(
            passage.is_supported_by(chunk.document_id, chunk.text) for passage in case.supporting
        )
    ]
