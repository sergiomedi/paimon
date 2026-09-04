"""Ingesting everything a source offers."""

from collections.abc import Sequence
from dataclasses import dataclass, field

import structlog

from paimon.application.use_cases.ingest_document import IngestDocument, SourceDocument
from paimon.domain.errors import IngestionError, SourceContentError, SourceError
from paimon.domain.ports import DocumentSource

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class SynchronizationResult:
    """What one pass over a source did.

    Attributes:
        source: Which source was read.
        indexed: Documents whose content had changed, or was new.
        unchanged: Documents that were already indexed as they stand.
        failed: Documents that could not be ingested, and why.
    """

    source: str
    indexed: int = 0
    unchanged: int = 0
    failed: Sequence[tuple[str, str]] = field(default_factory=tuple)

    @property
    def considered(self) -> int:
        """Everything the source offered."""
        return self.indexed + self.unchanged + len(self.failed)


class IngestSource:
    """Read every document a source offers and put it through ingestion.

    A separate use case rather than a loop inside the existing one, because the
    two answer different questions. ``IngestDocument`` decides what happens to a
    document; this decides what happens to a **run** — how far it gets when one
    document is malformed, and what it reports when it stops.

    One bad document does not end a synchronisation. A repository where a single
    file has been truncated should still index the other ninety-nine, and the
    failure should be visible rather than fatal. A source that cannot be reached
    at all is different: there is nothing to be partial about, and it is raised.
    """

    def __init__(self, ingest: IngestDocument) -> None:
        """Initialise the use case.

        Args:
            ingest: The per-document pipeline, unchanged. Everything a source
                brings in is parsed, chunked and embedded exactly as a document
                submitted over HTTP is — including the content hash, so a
                scheduled run over an unchanged repository costs no embeddings.
        """
        self._ingest = ingest

    async def __call__(self, source: DocumentSource, *, tenant_id: str) -> SynchronizationResult:
        """Synchronise one source into one tenant's corpus.

        Args:
            source: Where the documents come from.
            tenant_id: Whose corpus they belong to. Supplied by the caller from
                an authenticated principal, never by the source: a source has no
                say in whose material it becomes.

        Returns:
            What the run indexed, skipped and could not read.

        Raises:
            SourceUnavailableError: The source could not be reached at all.
            UntrustedSourceError: The source is not what it was registered as.
        """
        indexed = 0
        unchanged = 0
        failed: list[tuple[str, str]] = []

        async for reference in source.list():
            try:
                content = await source.fetch(reference)
            except SourceContentError as error:
                failed.append((reference.document_id, str(error)))
                continue

            try:
                result = await self._ingest(
                    SourceDocument(
                        tenant_id=tenant_id,
                        document_id=reference.document_id,
                        source_uri=reference.source_uri,
                        # Bytes from outside, treated as a document's text and
                        # nothing else. Whatever they say, they are never read
                        # as an instruction to this platform — the only thing
                        # that happens to them is parsing, chunking and
                        # indexing.
                        raw=content.raw,
                        media_type=reference.media_type,
                        metadata=reference.metadata,
                    )
                )
            except IngestionError as error:
                failed.append((reference.document_id, str(error)))
                continue

            if result.unchanged:
                unchanged += 1
            else:
                indexed += 1

        outcome = SynchronizationResult(
            source=source.name, indexed=indexed, unchanged=unchanged, failed=tuple(failed)
        )
        logger.info(
            "source_synchronized",
            source=outcome.source,
            indexed=outcome.indexed,
            unchanged=outcome.unchanged,
            failed=len(outcome.failed),
        )
        return outcome


__all__ = ["IngestSource", "SourceError", "SynchronizationResult"]
