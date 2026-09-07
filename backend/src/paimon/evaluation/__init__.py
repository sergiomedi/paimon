"""Measuring retrieval quality.

Pure with respect to infrastructure: the runner is given a retrieval use case and
a dataset, and knows nothing about where either comes from. The wiring lives in
the command-line interface, so the same benchmark runs against pgvector or Azure
AI Search without a line of it changing.

Every aggregate is reported with an interval, and two runs are compared
question by question rather than aggregate against aggregate (ADR-0029). A
mean with no interval is not a measurement, and on a dataset this size it is
an invitation to conclude something from noise.
"""

from paimon.evaluation.dataset import EvaluationCase, EvaluationDataset, SupportingPassage
from paimon.evaluation.metrics import CaseOutcome, RetrievalMetrics, score_case, summarize
from paimon.evaluation.runner import BenchmarkReport, run_benchmark
from paimon.evaluation.statistics import (
    Estimate,
    PairedDifference,
    clustered_estimate,
    estimate,
    paired_difference,
)

__all__ = [
    "BenchmarkReport",
    "CaseOutcome",
    "Estimate",
    "EvaluationCase",
    "EvaluationDataset",
    "PairedDifference",
    "RetrievalMetrics",
    "SupportingPassage",
    "clustered_estimate",
    "estimate",
    "paired_difference",
    "run_benchmark",
    "score_case",
    "summarize",
]
