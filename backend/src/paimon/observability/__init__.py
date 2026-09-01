"""Cross-cutting observability: structured logging now, tracing from Phase 5."""

from paimon.observability.logging import (
    CORRELATION_ID_HEADER,
    bind_correlation_id,
    clear_log_context,
    configure_logging,
    get_logger,
)

__all__ = [
    "CORRELATION_ID_HEADER",
    "bind_correlation_id",
    "clear_log_context",
    "configure_logging",
    "get_logger",
]
