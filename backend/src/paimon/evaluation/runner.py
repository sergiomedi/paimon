"""Running a retrieval benchmark."""

import time
from dataclasses import dataclass
from typing import Protocol

from paimon.domain.entities import Chunk
from paimon.domain.ports import SearchFilters
from paimon.evaluation.dataset import EvaluationCase, EvaluationDataset
from paimon.evaluation.metrics import CaseOutcome, RetrievalMetrics, score_case, summarize


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
    """

    dataset: str
    configuration: str
    metrics: RetrievalMetrics
    cases: tuple[CaseReport, ...]

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


async def run_benchmark(
    dataset: EvaluationDataset,
    retriever: Retriever,
    *,
    tenant_id: str,
    cutoff: int = 8,
    configuration: str = "unnamed",
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

    return BenchmarkReport(
        dataset=dataset.name,
        configuration=configuration,
        metrics=summarize(outcomes, ranks_per_case, cutoff),
        cases=tuple(reports),
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
