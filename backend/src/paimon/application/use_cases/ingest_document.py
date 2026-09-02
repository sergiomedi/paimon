"""Ingesting a document into the index."""

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field

from paimon.domain.entities import Document
from paimon.domain.errors import IngestionError
from paimon.domain.ports import (
    ChunkRecord,
    DocumentParser,
    DocumentRepository,
    EmbeddingModel,
    VectorStore,
)
from paimon.rag.chunking import Chunker


@dataclass(frozen=True, slots=True)
class SourceDocument:
    """A document offered for ingestion.

    Grouped into one value rather than passed as six arguments: they describe a
    single thing, and a caller that gets two of them in the wrong order would
    otherwise index a document under its own media type.

    Attributes:
        tenant_id: Owning organization.
        document_id: Stable identifier within the tenant. Re-using it replaces
            the previous version rather than adding a second copy.
        source_uri: Where the document came from.
        raw: The source bytes.
        media_type: Media type of the source.
        metadata: Extra provenance to carry alongside the document.
    """

    tenant_id: str
    document_id: str
    source_uri: str
    raw: bytes
    media_type: str
    metadata: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class IngestionResult:
    """What one ingestion did."""

    document_id: str
    chunks_indexed: int
    unchanged: bool


class IngestDocument:
    """Parse, chunk, embed and index a document.

    Idempotent by content *and* by pipeline. Re-ingesting unchanged bytes through
    an unchanged pipeline is a comparison and nothing else — no embedding calls,
    no index writes — which matters because a corpus is re-ingested on a schedule
    and embeddings are the expensive part.

    The pipeline is part of the comparison because content alone cannot decide it.
    The same document chunked at 512 tokens and at 256 produces different chunks,
    and an ingestion that skipped on content alone could never re-chunk anything —
    which is precisely the experiment the retrieval benchmark exists to run.
    """

    def __init__(
        self,
        parser: DocumentParser,
        repository: DocumentRepository,
        store: VectorStore,
        embedding_model: EmbeddingModel,
        chunker: Chunker,
    ) -> None:
        """Initialise the use case with the collaborators it needs."""
        self._parser = parser
        self._repository = repository
        self._store = store
        self._embedding_model = embedding_model
        self._chunker = chunker

    async def __call__(self, source: SourceDocument) -> IngestionResult:
        """Ingest one document.

        Args:
            source: The document to ingest.

        Returns:
            What was indexed, or that nothing needed to be.

        Raises:
            UnsupportedMediaTypeError: No parser handles this type.
            ParseError: The source could not be read.
            IngestionError: The document produced no chunks.
        """
        parsed = await self._parser.parse(source.raw, source.media_type)
        content_hash = hashlib.sha256(parsed.text.encode()).hexdigest()

        fingerprint = self._pipeline_fingerprint()

        existing = await self._repository.get(source.tenant_id, source.document_id)
        if (
            existing is not None
            and existing.content_hash == content_hash
            and existing.pipeline_fingerprint == fingerprint
        ):
            return IngestionResult(document_id=source.document_id, chunks_indexed=0, unchanged=True)

        document = Document(
            document_id=source.document_id,
            tenant_id=source.tenant_id,
            source_uri=source.source_uri,
            title=parsed.title or source.source_uri,
            text=parsed.text,
            content_hash=content_hash,
            media_type=source.media_type.split(";", maxsplit=1)[0].strip().lower(),
            pipeline_fingerprint=fingerprint,
            metadata={**parsed.metadata, **source.metadata},
        )

        chunks = self._chunker.split(document)
        if not chunks:
            msg = f"document '{source.document_id}' produced no chunks"
            raise IngestionError(msg)

        embeddings = await self._embedding_model.embed_documents(
            [chunk.embedding_text for chunk in chunks]
        )

        # Replace rather than merge: a shorter revision must not leave the chunks
        # it no longer contains in the index, answering questions from text that
        # has been deleted.
        await self._store.delete_document(source.tenant_id, source.document_id)
        await self._store.upsert(
            [
                ChunkRecord(chunk=chunk, embedding=embedding)
                for chunk, embedding in zip(chunks, embeddings, strict=True)
            ]
        )

        # Saved last, and deliberately. The stored hash is what marks the document
        # as ingested, so recording it only after the index is written means a run
        # that fails midway is retried rather than silently skipped.
        await self._repository.save(document)

        return IngestionResult(
            document_id=source.document_id, chunks_indexed=len(chunks), unchanged=False
        )

    def _pipeline_fingerprint(self) -> str:
        """Identify the chunking policy and embedding model in force."""
        return (
            f"{self._chunker.fingerprint}"
            f"|embed:{self._embedding_model.model_id}@{self._embedding_model.dimensions}"
        )
