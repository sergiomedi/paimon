"""Turning on the instrumentation that comes from libraries.

Two spans this platform does not have to write. The ASGI instrumentation opens a
server span per request, which is the root every other span in a request hangs
from; the httpx instrumentation opens a client span per outgoing call, which is
what turns "the request took nine seconds" into "the model took nine seconds".

Both are wired here rather than through the ``opentelemetry-instrument`` launcher.
Auto-instrumentation configured outside the process is configured somewhere this
repository does not show, and a reader who cannot see why a span exists cannot
reason about the ones that are missing.
"""

from fastapi import FastAPI
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.trace import TracerProvider

#: Health probes are excluded. A readiness check every few seconds from a load
#: balancer produces more spans than the traffic being watched, and paying to
#: store them buys a dashboard whose busiest endpoint is the one nobody uses.
EXCLUDED_URLS = "health/live,health/ready"


def instrument_server(app: FastAPI, provider: TracerProvider | None) -> None:
    """Open a server span for each request this application serves.

    Args:
        app: The application to instrument. Call after every route is registered:
            the instrumentation walks the routes to name spans by their path
            template, so ``/documents/{id}`` stays one span name rather than one
            per document.
        provider: The provider to record into, or None when tracing is off.
    """
    if provider is None:
        return
    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=provider,
        excluded_urls=EXCLUDED_URLS,
        # Four spans per request become one. The ASGI instrumentation opens a
        # child span for every `send` and `receive` message by default, which for
        # a plain JSON response is three spans describing the transport moving
        # bytes it was always going to move. They are not free — every one is
        # stored and billed by the backend — and on a streaming endpoint, where a
        # response is emitted step by step, the count grows with the answer.
        exclude_spans=["send", "receive"],
    )


def instrument_clients(provider: TracerProvider | None) -> None:
    """Open a client span for each outgoing HTTP call.

    Every model provider, every vector store over HTTP and the MCP client all
    reach the network through httpx, so this one call covers them — and covers
    the next one too, which is the argument for instrumenting the transport
    rather than each adapter.

    Args:
        provider: The provider to record into, or None when tracing is off.
    """
    if provider is None:
        return
    HTTPXClientInstrumentor().instrument(tracer_provider=provider)


def uninstrument_clients() -> None:
    """Undo :func:`instrument_clients`.

    Process-global state, so a test that builds two applications would otherwise
    instrument httpx twice and a suite would leak instrumentation between cases.
    Exposed for that, and used nowhere in production code.
    """
    HTTPXClientInstrumentor().uninstrument()


__all__ = [
    "EXCLUDED_URLS",
    "instrument_clients",
    "instrument_server",
    "uninstrument_clients",
]
