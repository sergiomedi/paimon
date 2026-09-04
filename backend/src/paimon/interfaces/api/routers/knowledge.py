"""Endpoints for putting documents in and getting grounded answers out."""

from fastapi import APIRouter, status

from paimon.application.use_cases import SourceDocument
from paimon.domain.errors import UnknownSourceError
from paimon.domain.ports import SearchFilters
from paimon.interfaces.api.dependencies import (
    AnswerQuestionDep,
    CurrentPrincipal,
    DocumentSourcesDep,
    IngestDocumentDep,
    IngestSourceDep,
)
from paimon.interfaces.api.schemas import (
    AnswerRequest,
    AnswerResponse,
    ErrorResponse,
    IngestDocumentRequest,
    IngestDocumentResponse,
    SourceListResponse,
    SynchronizationResponse,
)

router = APIRouter(tags=["knowledge"])


@router.put(
    "/documents/{document_id}",
    response_model=IngestDocumentResponse,
    summary="Ingest or replace a document",
    responses={
        status.HTTP_400_BAD_REQUEST: {"model": ErrorResponse},
        status.HTTP_415_UNSUPPORTED_MEDIA_TYPE: {"model": ErrorResponse},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorResponse},
    },
)
async def ingest_document(
    document_id: str,
    request: IngestDocumentRequest,
    principal: CurrentPrincipal,
    ingest: IngestDocumentDep,
) -> IngestDocumentResponse:
    """Parse, chunk, embed and index a document.

    PUT rather than POST because ingestion is idempotent by document id: sending
    the same document twice leaves the platform in the state one send would, and
    unchanged content does no work at all.
    """
    result = await ingest(
        SourceDocument(
            tenant_id=principal.tenant_id,
            document_id=document_id,
            source_uri=request.source_uri,
            raw=request.content.encode(),
            media_type=request.media_type,
            metadata=request.metadata,
        )
    )
    return IngestDocumentResponse.from_result(result)


@router.post(
    "/answers",
    response_model=AnswerResponse,
    summary="Answer a question from indexed material",
    responses={
        status.HTTP_401_UNAUTHORIZED: {"model": ErrorResponse},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorResponse},
    },
)
async def answer(
    request: AnswerRequest,
    principal: CurrentPrincipal,
    answer_question: AnswerQuestionDep,
) -> AnswerResponse:
    """Retrieve, then answer only from what was retrieved.

    A 200 with ``grounded: false`` is a normal outcome, not an error: it means
    the platform found nothing it could stand behind and said so. Treating that
    as a failure would push a caller towards retrying until it got an ungrounded
    answer instead.
    """
    result = await answer_question(
        request.question,
        SearchFilters(
            tenant_id=principal.tenant_id,
            document_ids=frozenset(request.document_ids) if request.document_ids else None,
        ),
    )
    return AnswerResponse.from_answer(result)


@router.get(
    "/sources",
    response_model=SourceListResponse,
    summary="List the external sources this deployment reads from",
)
async def list_sources(
    principal: CurrentPrincipal,
    sources: DocumentSourcesDep,
) -> SourceListResponse:
    """Name the sources a caller may synchronise.

    Configured, not discovered. What comes back is the registry this process was
    started with, which is also the complete set of servers it will ever dial.
    """
    _ = principal
    return SourceListResponse(sources=sorted(sources))


@router.post(
    "/sources/{name}/synchronizations",
    response_model=SynchronizationResponse,
    summary="Read a source and index everything it offers",
    responses={
        status.HTTP_404_NOT_FOUND: {"model": ErrorResponse},
        status.HTTP_502_BAD_GATEWAY: {"model": ErrorResponse},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorResponse},
    },
)
async def synchronize_source(
    name: str,
    principal: CurrentPrincipal,
    sources: DocumentSourcesDep,
    synchronize: IngestSourceDep,
) -> SynchronizationResponse:
    """Ingest every document a configured source offers.

    The tenant comes from the token, never from the request. A synchronisation
    puts somebody else's documents into somebody's corpus, and which corpus that
    is has to be a fact about the caller rather than a field they can set.

    Runs inline rather than as a background job. This is honest at the sizes the
    ceiling in configuration allows and stops being honest above them; a
    scheduled worker is Phase 7's problem, and the ceiling is what keeps the
    difference from becoming a surprise.
    """
    source = sources.get(name)
    if source is None:
        offered = ", ".join(sorted(sources)) or "none"
        msg = f"no source named '{name}'; this deployment offers: {offered}"
        raise UnknownSourceError(msg)
    result = await synchronize(source, tenant_id=principal.tenant_id)
    return SynchronizationResponse.from_result(result)
