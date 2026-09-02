"""Endpoints for putting documents in and getting grounded answers out."""

from fastapi import APIRouter, status

from paimon.application.use_cases import SourceDocument
from paimon.domain.ports import SearchFilters
from paimon.interfaces.api.dependencies import (
    AnswerQuestionDep,
    CurrentPrincipal,
    IngestDocumentDep,
)
from paimon.interfaces.api.schemas import (
    AnswerRequest,
    AnswerResponse,
    ErrorResponse,
    IngestDocumentRequest,
    IngestDocumentResponse,
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
