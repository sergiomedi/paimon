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
from dataclasses import asdict
from pathlib import Path

from paimon.application.use_cases import RetrieveChunks, SourceDocument
from paimon.config import get_settings
from paimon.domain.entities import Chunk
from paimon.domain.ports import SearchFilters
from paimon.evaluation import BenchmarkReport, EvaluationDataset, run_benchmark
from paimon.interfaces.api.dependencies import (
    Resources,
    build_ingest_document,
    build_resources,
    build_retrieve_chunks,
)
from paimon.observability import configure_logging, get_logger

logger = get_logger(__name__)
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


def read_corpus(corpus: Path) -> list[tuple[str, str, bytes, str]]:
    """Read a directory of documents. Blocking, so callers run it off the loop."""
    documents: list[tuple[str, str, bytes, str]] = []
    for path in sorted(corpus.iterdir()):
        media_type = MEDIA_TYPES.get(path.suffix.lower())
        if not path.is_file() or media_type is None:
            continue
        documents.append((path.stem, path.as_posix(), path.read_bytes(), media_type))
    return documents


async def ingest_corpus(resources: Resources, corpus: Path, tenant_id: str) -> int:
    """Ingest every supported document in a directory.

    Returns:
        How many documents were indexed or confirmed unchanged.
    """
    ingest = build_ingest_document(resources)
    documents = await asyncio.to_thread(read_corpus, corpus)
    count = 0
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
        count += 1
    return count


def render(report: BenchmarkReport) -> str:
    """Render a report for a terminal."""
    metrics = report.metrics
    lines = [
        "",
        f"dataset       {report.dataset}  ({metrics.cases} cases)",
        f"configuration {report.configuration}",
        f"cutoff        k={metrics.cutoff}",
        "",
        f"  answerable@k   {metrics.answerable_rate:6.1%}   "
        "at least one supporting passage retrieved",
        f"  recall@k       {metrics.recall_at_k:6.1%}   of expected passages retrieved",
        f"  precision@k    {metrics.precision_at_k:6.1%}   of the k slots that were useful",
        f"  MRR            {metrics.mean_reciprocal_rank:6.3f}   "
        "how high the first useful hit lands",
        f"  nDCG@k         {metrics.ndcg_at_k:6.3f}   rank-weighted quality",
        f"  median latency {report.median_latency_ms:6.1f} ms",
        "",
    ]
    if report.failures:
        lines.append("  retrieved nothing relevant:")
        lines.extend(f"    {case.outcome.case_id}  {case.question}" for case in report.failures)
        lines.append("")
    return "\n".join(lines)


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
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(settings.observability)

    dataset = EvaluationDataset.from_jsonl(args.dataset)

    async with build_resources(settings) as resources:
        if args.corpus:
            ingested = await ingest_corpus(resources, args.corpus, args.tenant)
            logger.info("corpus_ingested", documents=ingested)

        report = await run_benchmark(
            dataset,
            UseCaseRetriever(build_retrieve_chunks(resources)),
            tenant_id=args.tenant,
            cutoff=args.cutoff,
            configuration=args.label,
        )

    sys.stdout.write(render(report))

    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(asdict(report), indent=2), encoding="utf-8")

    return 0 if report.metrics.cases else 1


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(asyncio.run(main()))
