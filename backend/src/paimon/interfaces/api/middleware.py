"""ASGI middleware."""

from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from paimon.observability import CORRELATION_ID_HEADER, bind_correlation_id, clear_log_context


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Give every request a correlation id and echo it back.

    An id supplied by the caller is preserved, so a trace can be followed across
    service boundaries; otherwise one is generated. The context is cleared when
    the request finishes so that a recycled worker task cannot inherit the
    previous request's bindings and attribute its log lines to the wrong caller.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """Bind the correlation id for the duration of the request."""
        correlation_id = bind_correlation_id(request.headers.get(CORRELATION_ID_HEADER))
        request.state.correlation_id = correlation_id
        try:
            response = await call_next(request)
        finally:
            clear_log_context()
        response.headers[CORRELATION_ID_HEADER] = correlation_id
        return response
