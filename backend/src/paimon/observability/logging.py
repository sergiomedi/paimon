"""Structured logging.

Every log line is a JSON object carrying a correlation id, so that the records
belonging to one request can be reassembled from an aggregator without parsing
free text. Human-readable console output is available for local development and
is the only case where the format differs.

Logs emitted by libraries — uvicorn, SQLAlchemy, anything using the standard
library — are routed through the same processor chain. A deployment where the
application logs JSON and the web server logs prose is a deployment where half
the evidence is unqueryable.
"""

import logging
import sys
from uuid import uuid4

import structlog
from structlog.typing import Processor

from paimon.config import ObservabilitySettings

CORRELATION_ID_HEADER = "X-Correlation-ID"
"""Header carrying the correlation id in and out of the service."""

_LIBRARY_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access", "sqlalchemy.engine")


def configure_logging(settings: ObservabilitySettings) -> None:
    """Configure structlog and the standard library to share one pipeline.

    Safe to call more than once; the last call wins. Call it before anything else
    during startup, so that failures in the rest of the startup sequence are
    themselves logged in the configured format.

    Args:
        settings: Service name, level and output format.
    """
    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    renderer: Processor = (
        structlog.processors.JSONRenderer()
        if settings.log_format == "json"
        else structlog.dev.ConsoleRenderer(colors=True)
    )

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        # foreign_pre_chain is applied to records that did not originate in
        # structlog, which is how library logs acquire the same shape.
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            renderer,
        ],
    )

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.log_level)

    for name in _LIBRARY_LOGGERS:
        library_logger = logging.getLogger(name)
        library_logger.handlers = []
        library_logger.propagate = True

    structlog.contextvars.bind_contextvars(service=settings.service_name)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a bound logger for the given module name.

    Args:
        name: Usually ``__name__`` of the calling module.
    """
    return structlog.stdlib.get_logger(name)


def bind_correlation_id(correlation_id: str | None = None) -> str:
    """Bind a correlation id to the current context, generating one if absent.

    The id is context-local, so concurrent requests handled by the same worker do
    not see each other's value.

    Args:
        correlation_id: An id received from the caller, if it supplied one.

    Returns:
        The correlation id now bound to the context.
    """
    value = correlation_id or str(uuid4())
    structlog.contextvars.bind_contextvars(correlation_id=value)
    return value


def clear_log_context() -> None:
    """Discard every context-local binding.

    Called when a request finishes, so that a recycled worker task does not
    inherit the previous request's context.
    """
    structlog.contextvars.clear_contextvars()
