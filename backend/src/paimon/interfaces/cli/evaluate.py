"""Run the retrieval benchmark.

Usage, from the backend directory::

    uv run python -m paimon.interfaces.cli.evaluate
        --corpus ../evaluation/corpus/sample
        --dataset ../evaluation/datasets/retrieval-v1.jsonl
        --label "chunk=512 overlap=64 rrf=60"
"""

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

from paimon.application.use_cases import (
    Answer,
    AnswerQuestion,
    RetrieveChunks,
    SourceDocument,
)
from paimon.config import get_settings
from paimon.domain.entities import Chunk
from paimon.domain.ports import SearchFilters
from paimon.evaluation import (
    ACCEPTABLE_KAPPA,
    AnsweringReport,
    BenchmarkReport,
    Calibration,
    EvaluationDataset,
    JudgedMetrics,
    Judging,
    labelling_template,
    load_labels,
    run_answering_benchmark,
    run_benchmark,
)
from paimon.evaluation.metrics import RetrievalMetrics
from paimon.evaluation.statistics import Estimate
from paimon.interfaces.api.dependencies import (
    Resources,
    build_answer_judge,
    build_answer_question,
    build_ingest_document,
    build_resources,
    build_retrieve_chunks,
)
from paimon.observability import configure_logging, get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from _typeshed import DataclassInstance

logger = get_logger(__name__)

#: A comparison uses only the per-question scores, so a report read back from
#: disk carries no aggregates. Stated as a constant rather than rebuilt, so
#: nothing mistakes an unread field for a measured zero.
_NOTHING = Estimate(mean=0.0, standard_error=0.0, n=0)
EMPTY_METRICS = RetrievalMetrics(
    cases=0,
    cutoff=0,
    recall_at_k=_NOTHING,
    precision_at_k=_NOTHING,
    mean_reciprocal_rank=_NOTHING,
    ndcg_at_k=_NOTHING,
    answerable_rate=_NOTHING,
)
MEDIA_TYPES = {".md": "text/markdown", ".markdown": "text/markdown", ".txt": "text/plain"}


class UseCaseRetriever:
    """Adapts the retrieval use case to what the benchmark needs.

    The benchmark asks only for ranked chunks, so a single retriever could be
    measured in isolation later without changing anything here.
    """

    def __init__(self, retrieve: RetrieveChunks) -> None:
        """Wrap a retrieval use case."""
        self._retrieve = retrieve

    async def retrieve(self, question: str, filters: SearchFilters) -> list[Chunk]:
        """Return chunks for a question, best first."""
        result = await self._retrieve(question, filters)
        return [hit.chunk for hit in result.hits]


class UseCaseAnswerer:
    """Adapts the answering use case to what the benchmark needs."""

    def __init__(self, answer: AnswerQuestion) -> None:
        """Wrap an answering use case."""
        self._answer = answer

    async def answer(self, question: str, *, tenant_id: str) -> Answer:
        """Answer a question from the indexed corpus."""
        return await self._answer(question, SearchFilters(tenant_id=tenant_id))


def read_corpus(corpus: Path) -> list[tuple[str, str, bytes, str]]:
    """Read a directory of documents. Blocking, so callers run it off the loop."""
    documents: list[tuple[str, str, bytes, str]] = []
    for path in sorted(corpus.iterdir()):
        media_type = MEDIA_TYPES.get(path.suffix.lower())
        if not path.is_file() or media_type is None:
            continue
        documents.append((path.stem, path.as_posix(), path.read_bytes(), media_type))
    return documents


async def ingest_corpus(resources: Resources, corpus: Path, tenant_id: str) -> list[str]:
    """Ingest every supported document in a directory.

    Returns:
        The ids of the documents indexed or confirmed unchanged, in order. The
        ids rather than a count, because verifying an answer's citations needs
        the documents back and the repository has no "list everything" — nor
        should it, since nothing else in the platform ever wants one.
    """
    ingest = build_ingest_document(resources)
    documents = await asyncio.to_thread(read_corpus, corpus)
    ingested: list[str] = []
    for document_id, source_uri, raw, media_type in documents:
        result = await ingest(
            SourceDocument(
                tenant_id=tenant_id,
                document_id=document_id,
                source_uri=source_uri,
                raw=raw,
                media_type=media_type,
            )
        )
        logger.info(
            "corpus_document_ingested",
            document_id=result.document_id,
            chunks=result.chunks_indexed,
            unchanged=result.unchanged,
        )
        ingested.append(result.document_id)
    return ingested


async def load_documents(
    resources: Resources, document_ids: Sequence[str], tenant_id: str
) -> dict[str, str]:
    """Read back the corpus as it was indexed.

    The **normalized** text, not the file on disk. A citation's offsets are into
    what the parser produced, so checking them against the raw markdown would
    fail for every document the parser touched — headings, front matter, line
    endings — and the failures would look like the model inventing citations.
    """
    documents: dict[str, str] = {}
    for document_id in document_ids:
        document = await resources.document_repository.get(tenant_id, document_id)
        if document is not None:
            documents[document.document_id] = document.text
    return documents


def render(report: BenchmarkReport) -> str:
    """Render a report for a terminal.

    Every number carries its interval, and the header says how wide the dataset
    lets them be. A reader who sees ``73.3% +/- 21.4%`` will not conclude
    anything from a four-point move; a reader who sees ``73.3%`` very well might.
    """
    metrics = report.metrics
    lines = [
        "",
        f"dataset       {report.dataset}  ({metrics.cases} cases, "
        f"{metrics.recall_at_k.clusters or metrics.cases} independent groups)",
        f"configuration {report.configuration}",
        f"cutoff        k={metrics.cutoff}",
        "",
        "  metric            mean +/- 95% CI      what it says",
        f"  answerable@k   {metrics.answerable_rate.format(percent=True):>18}   "
        "at least one supporting passage retrieved",
        f"  recall@k       {metrics.recall_at_k.format(percent=True):>18}   "
        "of expected passages retrieved",
        f"  precision@k    {metrics.precision_at_k.format(percent=True):>18}   "
        "of the k slots that were useful",
        f"  MRR            {metrics.mean_reciprocal_rank.format():>18}   "
        "how high the first useful hit lands",
        f"  nDCG@k         {metrics.ndcg_at_k.format():>18}   rank-weighted quality",
        f"  median latency {report.median_latency_ms:14.1f} ms",
        "",
        "  Intervals are clustered by source document: questions about one",
        "  document are not independent observations of retrieval quality.",
        "",
    ]
    if report.failures:
        lines.append("  retrieved nothing relevant:")
        lines.extend(f"    {case.outcome.case_id}  {case.question}" for case in report.failures)
        lines.append("")
    return "\n".join(lines)


def render_answers(report: AnsweringReport) -> str:
    """Render an answering run.

    Every number here is **verified**, not judged: the citations were followed
    into the corpus and checked against the text they name. Nothing on this
    report is a model's opinion of another model, which is stated in the output
    because the distinction is the whole design (ADR-0030).
    """
    metrics = report.metrics
    lines = [
        "",
        f"dataset       {report.dataset}  ({metrics.cases} answers, "
        f"{metrics.citation_accuracy.clusters or metrics.cases} independent groups)",
        f"configuration {report.configuration}",
        "",
        "  metric                 mean +/- 95% CI      what it says",
        f"  grounded            {metrics.grounded_rate.format(percent=True):>18}   "
        "answers that cited anything at all",
        f"  citation accuracy   {metrics.citation_accuracy.format(percent=True):>18}   "
        "citations that survived being followed",
        f"  cited sentences     {metrics.cited_sentence_rate.format(percent=True):>18}   "
        "sentences carrying a marker",
        f"  fully attributed    {metrics.fully_attributed_rate.format(percent=True):>18}   "
        "every citation resolved, every sentence cited",
        "",
        "  Verified, not judged: each citation above was opened at its offsets",
        "  and checked against the text it names.",
        "",
    ]
    # Said only when it is true. The four numbers above are always verified; a
    # run with a judge has a second section that is not, and claiming no model
    # graded anything would be the exact kind of small lie this report exists to
    # avoid.
    if report.judged is None:
        lines[-2:] = [lines[-2], "  No model graded this run.", ""]
    if report.judged is not None:
        lines.extend(_judged_lines(report.judged))
    if report.unverifiable:
        lines.append("  citations that did not survive being followed:")
        for case in report.unverifiable:
            for check in case.attribution.checks:
                if not check.ok:
                    lines.append(
                        f"    {case.case_id}  [{check.marker}] {check.document_id}  "
                        f"{check.attribution.value}: {check.quote[:60]!r}"
                    )
        lines.append("")
    return "\n".join(lines)


def write_labelling_template(
    report: AnsweringReport, path: Path, dataset: EvaluationDataset
) -> None:
    """Write the cases out for a person to label.

    Everything the labeller needs is on the line, and that means **both** sets of
    evidence, because the three rubrics are graded against different things:
    ``sources`` is what the model was shown and is what faithfulness is judged
    against; ``expected_passages`` is what the golden set names and is what
    completeness is judged against; relevance needs neither. Giving a person one
    set of passages and three questions is asking them to grade two of the three
    off the wrong material (ADR-0033).

    The judge's verdict is deliberately absent. Showing it would anchor the
    labeller to it, and an independent measurement that has been anchored is an
    expensive way to confirm what the model already said.
    """
    expected = {case.case_id: case for case in dataset}
    rows = [
        {
            "case_id": case.case_id,
            "question": case.question,
            "answer": case.text,
            "sources": list(case.sources),
            "expected_passages": [passage.quote for passage in expected[case.case_id].supporting],
            "faithfulness": "",
            "completeness": "",
            "relevance": "",
            "note": "",
        }
        for case in report.cases
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(labelling_template(rows), encoding="utf-8")


def _calibration_lines(calibration: Calibration) -> list[str]:
    """Render what a person's labels said about the judge."""
    lines = [
        f"  calibrated against {calibration.labels} human labels",
        f"    faithfulness  {calibration.faithfulness.format()}",
        f"    completeness  {calibration.completeness.format()}",
        f"    relevance     {calibration.relevance.format()}",
        "",
    ]
    # Named, not aggregated. A judge is routinely trustworthy at one of these
    # rubrics and not at another — the first calibrated run of this platform came
    # back +1.00 on relevance and +0.45 on faithfulness — and a warning that said
    # only "calibration failed" would have sent somebody to fix the wrong rubric.
    failing = [
        name
        for name, agreement in (
            ("faithfulness", calibration.faithfulness),
            ("completeness", calibration.completeness),
            ("relevance", calibration.relevance),
        )
        if agreement.compared and not agreement.is_acceptable
    ]
    if failing:
        lines.extend(
            [
                f"  Kappa below {ACCEPTABLE_KAPPA} on: {', '.join(failing)}.",
                "  This judge and a person are not reliably measuring the same thing",
                "  there. Read those numbers as an indication, not a result, and fix",
                "  the rubric before quoting them. The rubrics that cleared the",
                "  threshold are unaffected.",
                "",
            ]
        )
    return lines


def _judged_lines(judged: JudgedMetrics) -> list[str]:
    """Render the judged section, kept visibly apart from the verified one.

    Its own heading, its own caveat, and the judge's name on it. These numbers
    are a model's opinion of another model's output; on the closest measured
    comparison such a judge agreed with human assessors 56% of the time and erred
    towards generosity. Printing them beside the verified numbers without saying
    so would be the mistake this whole design avoids.
    """
    lines = [
        f"  judged by {judged.judge_model}"
        + (f", {judged.samples} samples each" if judged.samples > 1 else ""),
        "",
        f"  faithfulness        {judged.faithfulness.format():>18}   "
        "stayed inside the sources it was shown",
        f"  completeness        {judged.completeness.format():>18}   "
        "carried what the golden passages say",
        f"  relevance           {judged.relevance.format():>18}   addressed the question asked",
        "",
        f"  {judged.judged} judged, {judged.undecided} undecided"
        + (f", {judged.disagreements} with samples that disagreed" if judged.samples > 1 else ""),
        "",
        "  A model's opinion of a model's output. On the closest measured",
        "  comparison a judge of this kind agreed with human assessors 56% of",
        "  the time, and erred towards saying the answer was supported.",
        "",
        "  Faithfulness is precision and completeness is recall, against",
        "  different evidence. Read them together: an answer that invents",
        "  nothing by saying nothing scores 1.0 on the first alone.",
    ]
    if judged.calibration is not None:
        lines.extend(_calibration_lines(judged.calibration))
    else:
        lines.append("  UNCALIBRATED: nobody has checked these verdicts against a person's,")
        lines.append("  so they are a figure rather than a measurement. See --write-labels.")
    if judged.self_judged:
        lines.append("  AND IT JUDGED ITSELF: the judge is the model that wrote these")
        lines.append("  answers, so these two numbers flatter it by an unknown amount.")
    lines.append("")
    return lines


def render_comparison(report: BenchmarkReport, baseline: BenchmarkReport) -> str:
    """Render a paired comparison against an earlier run.

    Paired, so the comparison uses the fact that both configurations answered the
    same questions. On fifteen questions that is most of the available
    information: the unpaired difference of two aggregates has a confidence
    interval wide enough to swallow any change worth making.
    """
    lines = [
        f"compared with  {baseline.configuration}  ({baseline.dataset})",
        "",
        f"  {'metric':<22}{'difference':>10}  {'95% CI':<22}{'p':>7}   verdict",
    ]
    for metric, percent in (
        ("answerable_rate", True),
        ("recall_at_k", True),
        ("precision_at_k", True),
        ("mean_reciprocal_rank", False),
        ("ndcg_at_k", False),
    ):
        difference = report.compare(baseline, metric)
        verdict = (
            "distinguishable from zero"
            if difference.is_significant()
            else "not distinguishable from noise"
        )
        low, high = difference.interval()
        if percent:
            mean, span = f"{difference.mean:+.1%}", f"[{low:+.1%}, {high:+.1%}]"
        else:
            mean, span = f"{difference.mean:+.3f}", f"[{low:+.3f}, {high:+.3f}]"
        lines.append(f"  {metric:<22}{mean:>10}  {span:<22}{difference.p_value:>7.3f}   {verdict}")

    correlation = report.compare(baseline).correlation
    lines.extend(
        [
            "",
            f"  The two configurations agreed about which questions were hard "
            f"(r={correlation:.2f}),",
            "  which is what pairing exploits. Comparing the aggregates instead would",
            "  widen every interval above.",
            "",
        ]
    )
    return "\n".join(lines)


def load_report(path: Path) -> BenchmarkReport:
    """Read a report written by an earlier run.

    Only the parts a comparison needs are rebuilt — the per-question scores and
    the clusters. Reconstructing the whole object would mean a second parser to
    keep in step with the dataclasses, for information a comparison does not use.

    Raises:
        ValueError: If the file predates per-question scores. An older report can
            still be read for its aggregates, and cannot be paired against —
            which has to be said rather than approximated.
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    scores = raw.get("scores")
    if not scores:
        msg = (
            f"'{path}' carries no per-question scores, so it cannot be compared "
            "question by question. Re-run the baseline to produce one."
        )
        raise ValueError(msg)
    return BenchmarkReport(
        dataset=raw["dataset"],
        configuration=raw["configuration"],
        metrics=EMPTY_METRICS,
        cases=(),
        scores={name: tuple(values) for name, values in scores.items()},
        clusters=tuple(raw.get("clusters", ())),
    )


async def main(argv: list[str] | None = None) -> int:
    """Ingest the corpus if asked, run the benchmark, report.

    Returns:
        Process exit status: non-zero when the run produced no cases, since an
        empty benchmark that reports success is worse than one that fails.
    """
    parser = argparse.ArgumentParser(description="Run the retrieval benchmark.")
    parser.add_argument("--corpus", type=Path, help="Directory of documents to ingest first.")
    parser.add_argument("--dataset", type=Path, required=True, help="Golden set, JSON Lines.")
    parser.add_argument("--tenant", default="benchmark", help="Tenant to ingest and query as.")
    parser.add_argument("--cutoff", type=int, default=8, help="The k metrics are measured at.")
    parser.add_argument(
        "--label",
        default="unnamed",
        help="What is being measured. A metric without its configuration is unattributable.",
    )
    parser.add_argument("--report", type=Path, help="Write the full report here as JSON.")
    parser.add_argument(
        "--answers",
        action="store_true",
        help=(
            "Benchmark the answers rather than the retrieval: run the answering "
            "use case and verify every citation against the corpus."
        ),
    )
    parser.add_argument(
        "--labels",
        type=Path,
        help=(
            "A file of human labels, to measure how far this judge agrees with a "
            "person. Without it the judged numbers are reported as uncalibrated."
        ),
    )
    parser.add_argument(
        "--write-labels",
        type=Path,
        help=(
            "Write a labelling template for this run and stop. Fill in the blank "
            "verdicts, then pass the file back with --labels."
        ),
    )
    parser.add_argument(
        "--against",
        type=Path,
        help=(
            "An earlier report to compare against, question by question. "
            "This is how a retrieval change is accepted or rejected."
        ),
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.observability)

    dataset = EvaluationDataset.from_jsonl(args.dataset)

    async with build_resources(settings) as resources:
        ingested: list[str] = []
        if args.corpus:
            ingested = await ingest_corpus(resources, args.corpus, args.tenant)
            logger.info("corpus_ingested", documents=len(ingested))

        if args.answers:
            answering = await run_answering_benchmark(
                dataset,
                UseCaseAnswerer(build_answer_question(resources)),
                await load_documents(resources, ingested, args.tenant),
                tenant_id=args.tenant,
                configuration=args.label,
                judging=Judging(
                    judge=build_answer_judge(resources),
                    self_judged=settings.evaluation.judge.acknowledge_self_judging,
                    labels=load_labels(args.labels) if args.labels else (),
                ),
            )
            if args.write_labels:
                write_labelling_template(answering, args.write_labels, dataset)
                sys.stdout.write(
                    f"\nwrote {len(answering.cases)} cases to {args.write_labels}\n"
                    "Fill in the three blank verdicts on each line (yes / partial / no):\n"
                    "  faithfulness  does the answer stay inside 'sources'?\n"
                    "  completeness  does it carry what 'expected_passages' say?\n"
                    "  relevance     does it answer the question at all?\n"
                    "Leave any verdict blank to skip it, then pass the file back "
                    "with --labels.\n\n"
                )
                return 0
            _emit(render_answers(answering), args.report, answering)
            return 0 if answering.metrics.cases else 1

        report = await run_benchmark(
            dataset,
            UseCaseRetriever(build_retrieve_chunks(resources)),
            tenant_id=args.tenant,
            cutoff=args.cutoff,
            configuration=args.label,
        )

    sys.stdout.write(render(report))

    if args.against:
        baseline = load_report(args.against)
        sys.stdout.write(render_comparison(report, baseline))

    _emit("", args.report, report)
    return 0 if report.metrics.cases else 1


def _emit(rendered: str, path: Path | None, report: "DataclassInstance") -> None:
    """Print what was rendered, and write the report if asked."""
    if rendered:
        sys.stdout.write(rendered)
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(report), indent=2, default=str), encoding="utf-8")


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(asyncio.run(main()))
