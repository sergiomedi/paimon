"""Measuring retrieval quality.

Pure with respect to infrastructure: the runner is given a retrieval use case and
a dataset, and knows nothing about where either comes from. The wiring lives in
the command-line interface, so the same benchmark runs against pgvector or Azure
AI Search without a line of it changing.

Every aggregate is reported with an interval, and two runs are compared
question by question rather than aggregate against aggregate (ADR-0029). A
mean with no interval is not a measurement, and on a dataset this size it is
an invitation to conclude something from noise.

Answer quality is **verified, not judged**: citations are followed into the
corpus and checked against the text they name, which this platform can do
because ADR-0013 made citations carry their offsets (ADR-0030).
"""

from paimon.evaluation.answering import (
    AnswerCaseReport,
    AnsweringMetrics,
    AnsweringReport,
    JudgedMetrics,
    Judging,
    run_answering_benchmark,
)
from paimon.evaluation.attribution import (
    Attribution,
    AttributionReport,
    CitationCheck,
    check_answer,
)
from paimon.evaluation.dataset import EvaluationCase, EvaluationDataset, SupportingPassage
from paimon.evaluation.judging import (
    AnswerJudge,
    JudgedAnswer,
    Judgement,
    Verdict,
)
from paimon.evaluation.metrics import CaseOutcome, RetrievalMetrics, score_case, summarize
from paimon.evaluation.model_judge import ModelAnswerJudge
from paimon.evaluation.runner import BenchmarkReport, run_benchmark
from paimon.evaluation.statistics import (
    Estimate,
    PairedDifference,
    clustered_estimate,
    estimate,
    paired_difference,
)

__all__ = [
    "AnswerCaseReport",
    "AnswerJudge",
    "AnsweringMetrics",
    "AnsweringReport",
    "Attribution",
    "AttributionReport",
    "BenchmarkReport",
    "CaseOutcome",
    "CitationCheck",
    "Estimate",
    "EvaluationCase",
    "EvaluationDataset",
    "JudgedAnswer",
    "JudgedMetrics",
    "Judgement",
    "Judging",
    "ModelAnswerJudge",
    "PairedDifference",
    "RetrievalMetrics",
    "SupportingPassage",
    "Verdict",
    "check_answer",
    "clustered_estimate",
    "estimate",
    "paired_difference",
    "run_answering_benchmark",
    "run_benchmark",
    "score_case",
    "summarize",
]
