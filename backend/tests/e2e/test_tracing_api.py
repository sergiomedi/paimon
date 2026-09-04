"""What a request looks like once it is traced.

The provider is handed to the instrumentation directly rather than installed
process-wide, which is why these functions take one: a test that set the global
provider would be deciding for every test that ran after it, and OpenTelemetry
refuses to let a second one correct the mistake.
"""

from collections.abc import AsyncIterator, Iterator

import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from paimon.infrastructure.identity import DevIdentityProvider
from paimon.observability import CORRELATION_ID_ATTRIBUTE, CORRELATION_ID_HEADER
from paimon.observability.instrumentation import instrument_server
from tests.conftest import DEV_SIGNING_KEY


@pytest.fixture
def spans(app: FastAPI) -> Iterator[InMemorySpanExporter]:
    """Instrument the application under test, recording into memory."""
    memory = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(memory))
    instrument_server(app, provider)
    yield memory
    provider.shutdown()


@pytest.fixture
async def traced(app: FastAPI, spans: InMemorySpanExporter) -> AsyncIterator[AsyncClient]:
    """A client against the instrumented application."""
    async with (
        LifespanManager(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client,
    ):
        yield client


def token() -> str:
    return DevIdentityProvider(signing_key=DEV_SIGNING_KEY).issue(
        subject="caller", tenant_id="tenant-1"
    )


def server_spans(memory: InMemorySpanExporter) -> list[ReadableSpan]:
    """Every span recorded so far."""
    return list(memory.get_finished_spans())


class TestARequestIsOneSpan:
    async def test_a_served_request_produces_a_span(
        self, traced: AsyncClient, spans: InMemorySpanExporter
    ) -> None:
        await traced.get("/api/v1/me", headers={"Authorization": f"Bearer {token()}"})
        assert server_spans(spans)

    async def test_the_span_is_named_by_the_route_not_the_url(
        self, traced: AsyncClient, spans: InMemorySpanExporter
    ) -> None:
        # A span per document id would make the busiest endpoint in any dashboard
        # a list of one-off names, and would make aggregation meaningless.
        await traced.get("/api/v1/me", headers={"Authorization": f"Bearer {token()}"})
        assert any("/api/v1/me" in span.name for span in server_spans(spans))


class TestTheJoinBetweenLogsAndTraces:
    async def test_the_caller_s_correlation_id_is_on_the_span(
        self, traced: AsyncClient, spans: InMemorySpanExporter
    ) -> None:
        # The join, end to end. The logs already carry this id; putting it on the
        # span is what lets a reader move from a log line to the timing and back.
        #
        # That it lands at all is also proof of an ordering that is easy to get
        # wrong: the tracing middleware has to be outermost, or the span does not
        # exist yet when the correlation middleware tries to write to it. It is,
        # because instrumentation is installed last in create_app and Starlette
        # adds each new middleware on the outside.
        response = await traced.get(
            "/api/v1/me",
            headers={"Authorization": f"Bearer {token()}", CORRELATION_ID_HEADER: "corr-xyz"},
        )
        assert response.headers[CORRELATION_ID_HEADER] == "corr-xyz"
        assert any(
            (span.attributes or {}).get(CORRELATION_ID_ATTRIBUTE) == "corr-xyz"
            for span in server_spans(spans)
        )

    async def test_a_request_without_one_still_gets_an_id_on_its_span(
        self, traced: AsyncClient, spans: InMemorySpanExporter
    ) -> None:
        await traced.get("/api/v1/me", headers={"Authorization": f"Bearer {token()}"})
        assert all(
            (span.attributes or {}).get(CORRELATION_ID_ATTRIBUTE) for span in server_spans(spans)
        )


class TestSpanVolume:
    """What is emitted per request, which is what a backend bills for."""

    async def test_a_request_is_one_span_and_not_four(
        self, traced: AsyncClient, spans: InMemorySpanExporter
    ) -> None:
        # The ASGI instrumentation opens a child span per `send` and `receive`
        # message by default: three extra spans describing the transport moving
        # bytes it was always going to move. On a streaming endpoint the count
        # grows with the length of the answer. Turned off in instrument_server,
        # and pinned here because it is the kind of default that comes back.
        await traced.get("/api/v1/me", headers={"Authorization": f"Bearer {token()}"})
        assert len(server_spans(spans)) == 1


class TestWhatIsNotTraced:
    async def test_health_probes_do_not_produce_spans(
        self, traced: AsyncClient, spans: InMemorySpanExporter
    ) -> None:
        # A readiness check every few seconds outnumbers the traffic it watches.
        # Storing those buys a dashboard whose busiest endpoint is the one nobody
        # uses, and hides the requests somebody actually made.
        await traced.get("/api/v1/health/live")
        await traced.get("/api/v1/health/ready")
        assert server_spans(spans) == []
