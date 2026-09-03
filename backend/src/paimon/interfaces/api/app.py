"""Application factory.

Assembles the HTTP interface: lifespan-managed resources, middleware, exception
handlers and routers. Concrete adapters are never referenced here — they are
built by the composition root in
:mod:`paimon.interfaces.api.dependencies`, which is the only module allowed to
import infrastructure.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from paimon.config import Settings, get_settings
from paimon.domain.errors import (
    DomainError,
    EmbeddingError,
    GenerationError,
    IdentityProviderUnavailableError,
    IngestionError,
    InvalidTokenError,
    UnsupportedMediaTypeError,
)
from paimon.interfaces.api.dependencies import build_agent_workflows, build_resources
from paimon.interfaces.api.middleware import CorrelationIdMiddleware
from paimon.interfaces.api.routers import agents, health, identity, knowledge
from paimon.interfaces.api.schemas import ErrorResponse
from paimon.observability import configure_logging, get_logger

API_PREFIX = "/api/v1"

logger = get_logger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application.

    Args:
        settings: Overrides the environment-derived settings. Used by tests; in a
            running process the argument is omitted and configuration comes from
            the environment, validated at startup.

    Returns:
        A configured application.
    """
    resolved = settings or get_settings()
    configure_logging(resolved.observability)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with build_resources(resolved) as resources:
            app.state.resources = resources
            # Compiled once, here, so a malformed graph aborts startup instead
            # of surfacing as a 500 to whoever first asks for that agent.
            app.state.agent_workflows = build_agent_workflows(resources)
            logger.info(
                "application_started",
                environment=resolved.environment.value,
                identity_provider=resolved.auth.provider,
            )
            yield
            logger.info("application_stopping")

    app = FastAPI(
        title="Paimon",
        version="0.1.0",
        summary="An AI Operations Platform for engineering organizations.",
        lifespan=lifespan,
        # Interactive docs are a development affordance; deployed environments
        # expose the schema through the gateway instead.
        docs_url="/docs" if not resolved.environment.is_deployed else None,
        redoc_url=None,
        openapi_url="/openapi.json" if not resolved.environment.is_deployed else None,
    )

    app.add_middleware(CorrelationIdMiddleware)
    _register_exception_handlers(app)

    app.include_router(health.router, prefix=API_PREFIX)
    app.include_router(identity.router, prefix=API_PREFIX)
    app.include_router(knowledge.router, prefix=API_PREFIX)
    app.include_router(agents.router, prefix=API_PREFIX)
    return app


def _error_response(request: Request, status_code: int, detail: str) -> JSONResponse:
    body = ErrorResponse(
        detail=detail,
        correlation_id=getattr(request.state, "correlation_id", None),
    )
    return JSONResponse(status_code=status_code, content=body.model_dump())


def _register_exception_handlers(app: FastAPI) -> None:
    """Translate domain errors into HTTP responses.

    This is the only place that knows both vocabularies. The domain raises
    meaning; the interface decides status codes.
    """

    @app.exception_handler(InvalidTokenError)
    async def _invalid_token(request: Request, exc: InvalidTokenError) -> JSONResponse:
        logger.info("authentication_failed", reason=str(exc))
        # The reason is logged, never returned: telling a caller *why* a token
        # was rejected helps them forge a better one.
        return _error_response(request, status.HTTP_401_UNAUTHORIZED, "invalid or missing token")

    @app.exception_handler(IdentityProviderUnavailableError)
    async def _provider_unavailable(
        request: Request, exc: IdentityProviderUnavailableError
    ) -> JSONResponse:
        # Not the caller's fault: we cannot tell whether the token is valid.
        # A 401 here would send clients to re-authenticate against a provider
        # that is already struggling.
        logger.error("identity_provider_unavailable", reason=str(exc))
        return _error_response(
            request,
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "identity provider unavailable",
        )

    @app.exception_handler(UnsupportedMediaTypeError)
    async def _unsupported_media_type(
        request: Request, exc: UnsupportedMediaTypeError
    ) -> JSONResponse:
        return _error_response(request, status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, str(exc))

    @app.exception_handler(IngestionError)
    async def _ingestion_error(request: Request, exc: IngestionError) -> JSONResponse:
        # The message describes what the caller sent, so returning it helps them
        # fix it and reveals nothing they did not already provide.
        logger.info("ingestion_rejected", reason=str(exc))
        return _error_response(request, status.HTTP_400_BAD_REQUEST, str(exc))

    @app.exception_handler(EmbeddingError)
    @app.exception_handler(GenerationError)
    async def _model_provider_unavailable(request: Request, exc: DomainError) -> JSONResponse:
        # Not the caller's fault and not permanent: a 503 tells a client to retry,
        # where a 500 tells it to give up.
        logger.error("model_provider_failed", error_type=type(exc).__name__, reason=str(exc))
        return _error_response(
            request, status.HTTP_503_SERVICE_UNAVAILABLE, "model provider unavailable"
        )

    @app.exception_handler(DomainError)
    async def _domain_error(request: Request, exc: DomainError) -> JSONResponse:
        logger.error("domain_error", error_type=type(exc).__name__, reason=str(exc))
        return _error_response(
            request,
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "internal error",
        )
