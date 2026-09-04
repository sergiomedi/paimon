"""Request and response models for the HTTP interface.

Deliberately separate from domain entities. A response model is a published
contract with clients; an entity is free to change with the business. Coupling
them means every domain refactor is a breaking API change.
"""

from pydantic import BaseModel, Field

from paimon.application.use_cases import (
    Answer,
    IngestionResult,
    ReadinessReport,
    SynchronizationResult,
)
from paimon.domain.entities import Principal
from paimon.domain.value_objects import Citation


class LivenessResponse(BaseModel):
    """Answer to a liveness probe."""

    status: str = Field(examples=["alive"])


class ComponentStatusResponse(BaseModel):
    """State of one dependency."""

    component: str = Field(examples=["postgresql"])
    healthy: bool
    latency_ms: float
    error: str | None = None


class ReadinessResponse(BaseModel):
    """Answer to a readiness probe."""

    ready: bool
    components: list[ComponentStatusResponse]

    @classmethod
    def from_report(cls, report: ReadinessReport) -> "ReadinessResponse":
        """Build the response from the use case's report."""
        return cls(
            ready=report.is_ready,
            components=[
                ComponentStatusResponse(
                    component=component.component,
                    healthy=component.healthy,
                    latency_ms=component.latency_ms,
                    error=component.error,
                )
                for component in report.components
            ],
        )


class PrincipalResponse(BaseModel):
    """The authenticated caller."""

    subject: str
    tenant_id: str
    display_name: str | None
    roles: list[str]

    @classmethod
    def from_principal(cls, principal: Principal) -> "PrincipalResponse":
        """Build the response from the domain entity."""
        return cls(
            subject=principal.subject,
            tenant_id=principal.tenant_id,
            display_name=principal.display_name,
            roles=sorted(principal.roles),
        )


class ErrorResponse(BaseModel):
    """Uniform error body."""

    detail: str
    correlation_id: str | None = None


class SourceListResponse(BaseModel):
    """The external sources this deployment is configured to read."""

    sources: list[str]


class FailedDocument(BaseModel):
    """One document a synchronisation could not index, and why."""

    document_id: str
    reason: str


class SynchronizationResponse(BaseModel):
    """What one pass over a source did.

    The failures are listed rather than counted. A run that indexed ninety-nine
    documents and could not read one has a name for the one, and a caller who
    cannot see which it was has to diff two corpora to find out.
    """

    source: str
    considered: int
    indexed: int
    unchanged: int
    failed: list[FailedDocument]

    @classmethod
    def from_result(cls, result: SynchronizationResult) -> "SynchronizationResponse":
        """Build the response from the use case's result."""
        return cls(
            source=result.source,
            considered=result.considered,
            indexed=result.indexed,
            unchanged=result.unchanged,
            failed=[
                FailedDocument(document_id=document_id, reason=reason)
                for document_id, reason in result.failed
            ],
        )


class IngestDocumentRequest(BaseModel):
    """A document offered for ingestion.

    The document id is the path, not a field: it identifies the resource being
    replaced, and accepting it in both places invites the two to disagree.
    """

    source_uri: str = Field(min_length=1, max_length=2048)
    content: str = Field(min_length=1, description="The document text.")
    media_type: str = "text/markdown"
    metadata: dict[str, str] = Field(default_factory=dict)


class IngestDocumentResponse(BaseModel):
    """What one ingestion did."""

    document_id: str
    chunks_indexed: int
    unchanged: bool = Field(
        description="True when the content hash matched and no work was needed."
    )

    @classmethod
    def from_result(cls, result: IngestionResult) -> "IngestDocumentResponse":
        """Build the response from the use case's result."""
        return cls(
            document_id=result.document_id,
            chunks_indexed=result.chunks_indexed,
            unchanged=result.unchanged,
        )


class AnswerRequest(BaseModel):
    """A question to answer from indexed material."""

    question: str = Field(min_length=1, max_length=4000)
    document_ids: list[str] | None = Field(
        default=None, description="Restrict retrieval to these documents."
    )


class CitationResponse(BaseModel):
    """A source an answer rests on.

    Carries the quoted span and the offsets it came from, so a client can both
    show the quote and open the document at the passage.
    """

    marker: int
    document_id: str
    chunk_id: str
    source_uri: str
    title: str
    heading_path: list[str]
    start_char: int
    end_char: int
    quote: str

    @classmethod
    def from_citation(cls, citation: Citation) -> "CitationResponse":
        """Build the response from the domain value object."""
        return cls(
            marker=citation.marker,
            document_id=citation.document_id,
            chunk_id=citation.chunk_id,
            source_uri=citation.source_uri,
            title=citation.title,
            heading_path=list(citation.heading_path),
            start_char=citation.start_char,
            end_char=citation.end_char,
            quote=citation.quote,
        )


class UsageResponse(BaseModel):
    """What answering the question cost."""

    model_id: str
    input_tokens: int
    output_tokens: int
    total_tokens: int


class AnswerResponse(BaseModel):
    """An answer and the evidence behind it."""

    answer: str
    grounded: bool = Field(description="Whether the answer cites anything the reader can check.")
    citations: list[CitationResponse]
    strategy: str = Field(description="How retrieval reached its candidates.")
    retrieved: int
    used_sources: int
    dropped_markers: list[int] = Field(
        default_factory=list,
        description="Markers the model used that referred to no source.",
    )
    usage: UsageResponse | None = None

    @classmethod
    def from_answer(cls, answer: Answer) -> "AnswerResponse":
        """Build the response from the use case's result."""
        return cls(
            answer=answer.text,
            grounded=answer.grounded,
            citations=[CitationResponse.from_citation(citation) for citation in answer.citations],
            strategy=answer.strategy,
            retrieved=answer.retrieved,
            used_sources=answer.used_sources,
            dropped_markers=list(answer.dropped_markers),
            usage=(
                UsageResponse(
                    model_id=answer.usage.model_id,
                    input_tokens=answer.usage.input_tokens,
                    output_tokens=answer.usage.output_tokens,
                    total_tokens=answer.usage.total_tokens,
                )
                if answer.usage
                else None
            ),
        )
