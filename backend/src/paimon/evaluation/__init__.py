"""Measuring retrieval quality.

Pure with respect to infrastructure: the runner is given a retrieval use case and
a dataset, and knows nothing about where either comes from. The wiring lives in
the command-line interface, so the same benchmark runs against pgvector or Azure
AI Search without a line of it changing.
"""

from paimon.evaluation.dataset import EvaluationCase, EvaluationDataset, SupportingPassage
from paimon.evaluation.metrics import CaseOutcome, RetrievalMetrics, score_case, summarize
from paimon.evaluation.runner import BenchmarkReport, run_benchmark

__all__ = [
    "BenchmarkReport",
    "CaseOutcome",
    "EvaluationCase",
    "EvaluationDataset",
    "RetrievalMetrics",
    "SupportingPassage",
    "run_benchmark",
    "score_case",
    "summarize",
]
