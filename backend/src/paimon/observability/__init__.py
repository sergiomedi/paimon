"""Cross-cutting observability: structured logging and tracing.

Both are configured once at startup and then used implicitly. Nothing outside
infrastructure and the composition root imports either — what is worth tracing is
what crosses a port, and the domain crosses none (ADR-0025).
"""

from paimon.observability.logging import (
    CORRELATION_ID_HEADER,
    bind_correlation_id,
    clear_log_context,
    configure_logging,
    get_logger,
)
from paimon.observability.tracing import (
    CORRELATION_ID_ATTRIBUTE,
    INSTRUMENTATION_SCOPE,
    annotate_current_span,
    build_tracer_provider,
    current_trace_context,
    get_tracer,
    record_error,
)
from paimon.observability.tracing import (
    install as install_tracer_provider,
)
from paimon.observability.tracing import (
    shutdown as shutdown_tracer_provider,
)

__all__ = [
    "CORRELATION_ID_ATTRIBUTE",
    "CORRELATION_ID_HEADER",
    "INSTRUMENTATION_SCOPE",
    "annotate_current_span",
    "bind_correlation_id",
    "build_tracer_provider",
    "clear_log_context",
    "configure_logging",
    "current_trace_context",
    "get_logger",
    "get_tracer",
    "install_tracer_provider",
    "record_error",
    "shutdown_tracer_provider",
]
