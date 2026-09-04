"""Tracing, and the one decision that shapes everything built on it.

The platform emits **plain OpenTelemetry** and nothing else. Langfuse accepts
OTLP and understands the ``gen_ai.*`` conventions directly, so it is a
destination rather than a dependency — an endpoint and a credential in
configuration. Azure Monitor, a collector, or a second backend alongside the
first are the same change. Reaching for a vendor's SDK would put that vendor's
name in call sites all over the platform, in the phase whose entire point is
being able to see what happens without being owned by whoever shows it
(ADR-0025).

**Nothing above infrastructure imports this module, and that is deliberate.**
What is worth tracing is what crosses a port: a model call, a vector search, an
HTTP request, a node in a graph. The domain performs no I/O, so it has nothing
to trace, and a ``Tracer`` port threaded through use cases would buy an
abstraction over an API that is already an abstraction — paid for in every
signature it passed through.

Off by default. A deployment with nowhere to send traces gets the API's own no-op
tracer, which is cheaper than an exporter that collects and discards.
"""

from collections.abc import Mapping

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ALWAYS_ON, ParentBased, Sampler, TraceIdRatioBased
from opentelemetry.semconv.resource import ResourceAttributes
from opentelemetry.trace import Span, Tracer

from paimon.config import Environment, TracingSettings

#: Name every span this platform creates is attributed to, so a backend can tell
#: our instrumentation from a library's.
INSTRUMENTATION_SCOPE = "paimon"

#: Correlation id, carried on the root span. The logs already have it, and this
#: is what lets a reader move between a log line and the trace it belongs to.
CORRELATION_ID_ATTRIBUTE = "paimon.correlation_id"


def build_tracer_provider(
    settings: TracingSettings, *, service_name: str, service_version: str, environment: Environment
) -> TracerProvider | None:
    """Build the provider for this process, or None when tracing is off.

    Args:
        settings: Whether to trace, where to send it, and how much to keep.
        service_name: Value for ``service.name``, which is how a backend groups
            spans into one service.
        service_version: Value for ``service.version``, so a regression can be
            attributed to a release rather than to a week.
        environment: Deployment environment, so staging traces and production
            traces are not read as one population.

    Returns:
        A configured provider, or None if tracing is disabled.
    """
    if not settings.enabled or not settings.endpoint:
        return None

    resource = Resource.create(
        {
            ResourceAttributes.SERVICE_NAME: service_name,
            ResourceAttributes.SERVICE_VERSION: service_version,
            ResourceAttributes.DEPLOYMENT_ENVIRONMENT: environment.value,
        }
    )
    provider = TracerProvider(resource=resource, sampler=_sampler(settings.sample_ratio))
    provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(
                endpoint=settings.endpoint,
                headers=dict(settings.headers),
                timeout=int(settings.export_timeout_seconds),
            )
        )
    )
    return provider


def _sampler(ratio: float) -> Sampler:
    """Choose a sampler for a keep ratio.

    ``ParentBased`` because a sampling decision belongs to a *trace*, not to a
    span: a service that re-decides half way through produces traces with holes
    in them, which are worse than traces that were never kept.
    """
    if ratio >= 1.0:
        return ParentBased(root=ALWAYS_ON)
    return ParentBased(root=TraceIdRatioBased(ratio))


def install(provider: TracerProvider | None) -> None:
    """Make a provider the process-wide default.

    Args:
        provider: The provider to install. None leaves the API's no-op in place,
            which is what "tracing is off" means: every ``start_as_current_span``
            elsewhere still runs and costs almost nothing.
    """
    if provider is not None:
        trace.set_tracer_provider(provider)


def shutdown(provider: TracerProvider | None) -> None:
    """Flush and stop a provider at shutdown.

    Without this the last batch dies with the process, which is exactly the batch
    containing whatever went wrong on the way out.
    """
    if provider is not None:
        provider.shutdown()


def get_tracer() -> Tracer:
    """Return this platform's tracer.

    Resolved per call rather than captured at import: the provider is installed
    during startup, and a tracer taken at import time would be bound to the no-op
    that preceded it — tracing configured and silent, with nothing to explain it.
    """
    return trace.get_tracer(INSTRUMENTATION_SCOPE)


def annotate_current_span(attributes: Mapping[str, str]) -> None:
    """Add attributes to whatever span is current, if any.

    Silently does nothing when no span is recording, so a caller never has to ask
    whether tracing is on.
    """
    span = trace.get_current_span()
    if not span.is_recording():
        return
    for key, value in attributes.items():
        span.set_attribute(key, value)


def record_error(span: Span, error: BaseException) -> None:
    """Mark a span as failed, the way the conventions ask for.

    Both halves matter: ``error.type`` is what a backend groups and alerts on,
    and the recorded exception is what a person reads once an alert has fired.
    """
    span.set_attribute("error.type", type(error).__qualname__)
    span.set_status(trace.Status(trace.StatusCode.ERROR, str(error)))
    span.record_exception(error)


def current_trace_context() -> dict[str, str]:
    """Return the current trace and span ids, for logging.

    Empty when nothing is being traced, so a log line gains the fields only when
    there is a trace to point at rather than carrying zeroes.
    """
    context = trace.get_current_span().get_span_context()
    if not context.is_valid:
        return {}
    return {
        "trace_id": trace.format_trace_id(context.trace_id),
        "span_id": trace.format_span_id(context.span_id),
    }


__all__ = [
    "CORRELATION_ID_ATTRIBUTE",
    "INSTRUMENTATION_SCOPE",
    "annotate_current_span",
    "build_tracer_provider",
    "current_trace_context",
    "get_tracer",
    "install",
    "record_error",
    "shutdown",
]
