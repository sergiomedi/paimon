"""The composition root.

This is the single module where concrete adapters are bound to domain ports, and
consequently the only module in the interfaces layer permitted to import from
infrastructure. The rule is enforced by import-linter (ADR-0002).

Routers depend on **use cases and ports**, never on adapters. The providers here
build the object graph; everything downstream receives it already assembled,
which is what keeps the dependency inversion real rather than nominal.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Protocol, runtime_checkable

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from paimon.application.use_cases import (
    AnswerQuestion,
    CheckReadiness,
    IngestDocument,
    RetrievalPolicy,
    RetrieveChunks,
)
from paimon.config import Settings
from paimon.domain.entities import Principal
from paimon.domain.errors import InvalidTokenError
from paimon.domain.ports import (
    ChatModel,
    DocumentParser,
    DocumentRepository,
    EmbeddingModel,
    HealthProbe,
    IdentityProvider,
    IndexDescriptor,
    TokenCounter,
    VectorStore,
)
from paimon.infrastructure.azure import build_credential
from paimon.infrastructure.azure.openai import (
    COGNITIVE_SERVICES_SCOPE,
    AzureOpenAIChatModel,
    AzureOpenAIConfig,
    AzureOpenAIEmbeddingModel,
)
from paimon.infrastructure.azure.search import (
    SEARCH_SCOPE,
    AzureSearchConfig,
    AzureSearchStore,
)
from paimon.infrastructure.cache import RedisHealthProbe, build_redis_client
from paimon.infrastructure.chat import OpenAICompatibleChatConfig, OpenAICompatibleChatModel
from paimon.infrastructure.embedding import (
    OpenAICompatibleConfig,
    OpenAICompatibleEmbeddingModel,
)
from paimon.infrastructure.identity import build_identity_provider
from paimon.infrastructure.parsing import MarkdownParser
from paimon.infrastructure.persistence import (
    PgVectorStore,
    PostgresDocumentRepository,
    PostgresHealthProbe,
    build_engine,
)
from paimon.infrastructure.tokenization import HeuristicTokenCounter
from paimon.rag.chunking import Chunker, ChunkingPolicy


@dataclass(frozen=True, slots=True)
class Resources:
    """Process-lifetime objects, built at startup and released at shutdown."""

    settings: Settings
    engine: AsyncEngine
    redis: Redis
    identity_provider: IdentityProvider
    readiness_probes: tuple[HealthProbe, ...]
    embedding_model: EmbeddingModel
    chat_model: ChatModel
    vector_store: VectorStore
    document_repository: DocumentRepository
    parser: DocumentParser
    chunker: Chunker
    token_counter: TokenCounter


@runtime_checkable
class _Closeable(Protocol):
    """Anything holding a connection pool that must be released at shutdown.

    Not on the ports: closing is a lifecycle concern of a particular
    implementation, and requiring it of every adapter would force an empty method
    onto in-memory ones. The composition root owns lifecycles, so it is the right
    place to ask.
    """

    async def aclose(self) -> None: ...


async def _close(resource: object) -> None:
    """Release a resource if it holds anything."""
    if isinstance(resource, _Closeable):
        await resource.aclose()


def _build_embedding_model(settings: Settings) -> EmbeddingModel:
    """Select the embedding adapter (ADR-0014)."""
    if settings.embedding.provider == "azure":
        azure = settings.azure_openai
        # Both are guaranteed by the startup validator; asserted so the type
        # checker knows it too.
        assert azure.endpoint  # noqa: S101
        assert azure.embedding_deployment  # noqa: S101
        return AzureOpenAIEmbeddingModel(
            AzureOpenAIConfig(
                endpoint=azure.endpoint,
                deployment=azure.embedding_deployment,
                api_version=azure.api_version,
                dimensions=settings.embedding.dimensions,
                document_prefix=settings.embedding.document_prefix,
                query_prefix=settings.embedding.query_prefix,
                batch_size=settings.embedding.batch_size,
                timeout_seconds=settings.embedding.timeout_seconds,
            ),
            build_credential(
                azure.api_key.get_secret_value() if azure.api_key else None,
                COGNITIVE_SERVICES_SCOPE,
            ),
        )
    return OpenAICompatibleEmbeddingModel(
        OpenAICompatibleConfig(
            base_url=settings.embedding.base_url,
            model=settings.embedding.model,
            dimensions=settings.embedding.dimensions,
            api_key=(
                settings.embedding.api_key.get_secret_value()
                if settings.embedding.api_key
                else None
            ),
            document_prefix=settings.embedding.document_prefix,
            query_prefix=settings.embedding.query_prefix,
            batch_size=settings.embedding.batch_size,
            timeout_seconds=settings.embedding.timeout_seconds,
        )
    )


def _build_chat_model(settings: Settings) -> ChatModel:
    """Select the generation adapter (ADR-0014)."""
    if settings.chat.provider == "azure":
        azure = settings.azure_openai
        assert azure.endpoint  # noqa: S101  guaranteed by the startup validator
        assert azure.chat_deployment  # noqa: S101
        return AzureOpenAIChatModel(
            AzureOpenAIConfig(
                endpoint=azure.endpoint,
                deployment=azure.chat_deployment,
                api_version=azure.api_version,
                timeout_seconds=settings.chat.timeout_seconds,
            ),
            build_credential(
                azure.api_key.get_secret_value() if azure.api_key else None,
                COGNITIVE_SERVICES_SCOPE,
            ),
            temperature=settings.chat.temperature,
            max_output_tokens=settings.chat.max_output_tokens,
        )
    return OpenAICompatibleChatModel(
        OpenAICompatibleChatConfig(
            base_url=settings.chat.base_url,
            model=settings.chat.model,
            api_key=settings.chat.api_key.get_secret_value() if settings.chat.api_key else None,
            temperature=settings.chat.temperature,
            max_output_tokens=settings.chat.max_output_tokens,
            timeout_seconds=settings.chat.timeout_seconds,
        )
    )


def _build_vector_store(
    settings: Settings, engine: AsyncEngine, embedding_model: EmbeddingModel
) -> VectorStore:
    """Select the retrieval backend (ADR-0003).

    The index is described by the model that fills it, not by the model named in
    configuration, so a mismatched pair is refused on the first write rather than
    producing an index nobody can query.
    """
    if settings.retrieval.store == "azure_search":
        azure = settings.azure_search
        assert azure.endpoint  # noqa: S101  guaranteed by the startup validator
        return AzureSearchStore(
            AzureSearchConfig(
                endpoint=azure.endpoint,
                index_name=azure.index_name,
                embedding_model_id=embedding_model.model_id,
                dimensions=embedding_model.dimensions,
                api_version=azure.api_version,
                semantic_configuration=azure.semantic_configuration,
            ),
            build_credential(
                azure.api_key.get_secret_value() if azure.api_key else None, SEARCH_SCOPE
            ),
        )
    return PgVectorStore(
        engine,
        IndexDescriptor(
            name=settings.embedding.index_name,
            embedding_model_id=embedding_model.model_id,
            dimensions=embedding_model.dimensions,
        ),
    )


@asynccontextmanager
async def build_resources(settings: Settings) -> AsyncIterator[Resources]:
    """Construct every long-lived dependency and release it on exit.

    Connections are not opened here. The engine and the Redis client connect
    lazily, so a database that is briefly unavailable delays readiness instead of
    preventing the process from starting — which is what lets an orchestrator
    restart dependencies in any order.

    Args:
        settings: Validated application settings.

    Yields:
        The assembled resources.
    """
    engine = build_engine(settings.database)
    redis = build_redis_client(settings.redis)
    embedding_model = _build_embedding_model(settings)
    chat_model = _build_chat_model(settings)
    token_counter = HeuristicTokenCounter()
    vector_store = _build_vector_store(settings, engine, embedding_model)
    try:
        yield Resources(
            settings=settings,
            engine=engine,
            redis=redis,
            identity_provider=build_identity_provider(settings.auth, settings.environment),
            readiness_probes=(PostgresHealthProbe(engine), RedisHealthProbe(redis)),
            embedding_model=embedding_model,
            chat_model=chat_model,
            vector_store=vector_store,
            document_repository=PostgresDocumentRepository(engine),
            parser=MarkdownParser(),
            chunker=Chunker(
                ChunkingPolicy(
                    max_tokens=settings.ingestion.max_chunk_tokens,
                    overlap_tokens=settings.ingestion.chunk_overlap_tokens,
                    min_tokens=settings.ingestion.min_chunk_tokens,
                ),
                token_counter,
            ),
            token_counter=token_counter,
        )
    finally:
        await _close(vector_store)
        await _close(chat_model)
        await _close(embedding_model)
        await redis.aclose()
        await engine.dispose()


def get_resources(request: Request) -> Resources:
    """Return the resources bound to the running application."""
    resources: Resources = request.app.state.resources
    return resources


ResourcesDep = Annotated[Resources, Depends(get_resources)]


def get_settings_dependency(resources: ResourcesDep) -> Settings:
    """Return the validated application settings."""
    return resources.settings


def get_identity_provider(resources: ResourcesDep) -> IdentityProvider:
    """Return the configured identity adapter, as the port."""
    return resources.identity_provider


def get_check_readiness(resources: ResourcesDep) -> CheckReadiness:
    """Return the readiness use case, wired to the configured probes."""
    return CheckReadiness(resources.readiness_probes)


# The builders below take Resources rather than a request, so anything holding a
# Resources can assemble the same object graph — the benchmark command builds the
# very use cases the API serves, instead of a lookalike that can drift from them.
def build_ingest_document(resources: Resources) -> IngestDocument:
    """Assemble the ingestion use case."""
    return IngestDocument(
        parser=resources.parser,
        repository=resources.document_repository,
        store=resources.vector_store,
        embedding_model=resources.embedding_model,
        chunker=resources.chunker,
    )


def build_retrieve_chunks(resources: Resources) -> RetrieveChunks:
    """Assemble the retrieval use case."""
    retrieval = resources.settings.retrieval
    return RetrieveChunks(
        store=resources.vector_store,
        embedding_model=resources.embedding_model,
        policy=RetrievalPolicy(
            top_k=retrieval.top_k,
            candidates_per_retriever=retrieval.candidates_per_retriever,
            rrf_k=retrieval.rrf_k,
        ),
    )


def build_answer_question(resources: Resources) -> AnswerQuestion:
    """Assemble the answering use case."""
    return AnswerQuestion(
        retrieve=build_retrieve_chunks(resources),
        chat_model=resources.chat_model,
        repository=resources.document_repository,
        token_counter=resources.token_counter,
        max_context_tokens=resources.settings.retrieval.max_context_tokens,
    )


# auto_error=False so that a missing header produces our own error shape rather
# than FastAPI's, keeping every authentication failure identical to the client.
_bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_principal(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)],
    identity_provider: Annotated[IdentityProvider, Depends(get_identity_provider)],
) -> Principal:
    """Authenticate the caller from the Authorization header.

    Args:
        credentials: Parsed bearer credentials, if the header was present.
        identity_provider: The configured adapter.

    Returns:
        The authenticated caller.

    Raises:
        InvalidTokenError: No usable token was presented, or verification failed.
            Translated to a 401 by the application's exception handler.
    """
    if credentials is None or not credentials.credentials:
        msg = "missing bearer token"
        raise InvalidTokenError(msg)
    return await identity_provider.authenticate(credentials.credentials)


def get_ingest_document(resources: ResourcesDep) -> IngestDocument:
    """Return the ingestion use case for a request."""
    return build_ingest_document(resources)


def get_retrieve_chunks(resources: ResourcesDep) -> RetrieveChunks:
    """Return the retrieval use case for a request."""
    return build_retrieve_chunks(resources)


def get_answer_question(resources: ResourcesDep) -> AnswerQuestion:
    """Return the answering use case for a request."""
    return build_answer_question(resources)


AnswerQuestionDep = Annotated[AnswerQuestion, Depends(get_answer_question)]
CheckReadinessDep = Annotated[CheckReadiness, Depends(get_check_readiness)]
IngestDocumentDep = Annotated[IngestDocument, Depends(get_ingest_document)]
CurrentPrincipal = Annotated[Principal, Depends(get_current_principal)]
SettingsDep = Annotated[Settings, Depends(get_settings_dependency)]
