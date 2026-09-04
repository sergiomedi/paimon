"""The tracing seam, exercised against a real SDK with an in-memory exporter.

No mocks. The questions worth asking here — is a span produced, does it carry the
resource attributes a backend groups by, does turning tracing off actually cost
nothing — are questions about the SDK's behaviour, and a mock of the SDK would
only confirm what this platform already believes about it.
"""

from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Tracer
from pydantic import ValidationError

from paimon.config import Environment, TracingSettings
from paimon.observability import (
    CORRELATION_ID_ATTRIBUTE,
    annotate_current_span,
    build_tracer_provider,
    current_trace_context,
    get_tracer,
    install_tracer_provider,
    record_error,
    shutdown_tracer_provider,
)

ENDPOINT = "https://collector.example.test/v1/traces"


@dataclass(frozen=True, slots=True)
class Recorder:
    """A tracer whose spans land in memory.

    Deliberately **not** installed as the process-wide provider. OpenTelemetry
    allows that to be set once and refuses later attempts, so a test that
    installed one would be deciding for every test that ran after it. The helpers
    under test read the *current span*, which is context-local, so a local tracer
    exercises them exactly as an installed one would.
    """

    tracer: Tracer
    spans: InMemorySpanExporter


@pytest.fixture
def recorder() -> Iterator[Recorder]:
    """A tracer recording into memory, torn down with the test."""
    memory = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(memory))
    yield Recorder(tracer=provider.get_tracer("paimon"), spans=memory)
    provider.shutdown()


class TestBuildingTheProvider:
    def test_it_is_off_unless_asked_for(self) -> None:
        # The default. A deployment with nowhere to send traces should pay
        # nothing, and None is what "use the API's no-op" looks like here.
        assert (
            build_tracer_provider(
                TracingSettings(),
                service_name="paimon-api",
                service_version="1.2.3",
                environment=Environment.LOCAL,
            )
            is None
        )

    def test_enabling_it_without_an_endpoint_is_refused_at_startup(self) -> None:
        # Rather than starting and silently collecting into nothing, which looks
        # identical to tracing that works until somebody opens the backend.
        with pytest.raises(ValidationError, match="nowhere to send"):
            TracingSettings(enabled=True)

    def test_it_carries_what_a_backend_groups_by(self) -> None:
        provider = build_tracer_provider(
            TracingSettings(enabled=True, endpoint=ENDPOINT),
            service_name="paimon-api",
            service_version="1.2.3",
            environment=Environment.STAGING,
        )
        assert provider is not None
        attributes = provider.resource.attributes
        assert attributes["service.name"] == "paimon-api"
        # Version, so a regression is attributed to a release and not to a week.
        assert attributes["service.version"] == "1.2.3"
        # Environment, so staging traces and production traces are not read as
        # one population.
        assert attributes["deployment.environment"] == "staging"
        shutdown_tracer_provider(provider)

    def test_a_sample_ratio_outside_zero_to_one_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            TracingSettings(sample_ratio=1.5)


class TestSpans:
    def test_a_span_is_recorded_with_its_name(self, recorder: Recorder) -> None:
        with recorder.tracer.start_as_current_span("chat gpt-4o-mini"):
            pass
        assert [span.name for span in recorder.spans.get_finished_spans()] == ["chat gpt-4o-mini"]

    def test_a_failure_is_recorded_as_one(self, recorder: Recorder) -> None:
        # error.type is what a backend groups and alerts on; the exception is
        # what a person reads once the alert has fired. Either alone is thin.
        with recorder.tracer.start_as_current_span("chat") as span:
            record_error(span, TimeoutError("the provider did not answer"))
        recorded = recorder.spans.get_finished_spans()[0]
        assert recorded.attributes is not None
        assert recorded.attributes["error.type"] == "TimeoutError"
        assert recorded.status.status_code is trace.StatusCode.ERROR
        assert [event.name for event in recorded.events] == ["exception"]

    def test_an_annotation_lands_on_the_current_span(self, recorder: Recorder) -> None:
        with recorder.tracer.start_as_current_span("request"):
            annotate_current_span({CORRELATION_ID_ATTRIBUTE: "corr-1"})
        recorded = recorder.spans.get_finished_spans()[0]
        assert recorded.attributes is not None
        assert recorded.attributes[CORRELATION_ID_ATTRIBUTE] == "corr-1"

    def test_annotating_without_a_span_is_not_an_error(self) -> None:
        # So no call site has to ask whether tracing is on, which is what keeps
        # `if tracing_enabled:` out of the code doing the actual work.
        annotate_current_span({CORRELATION_ID_ATTRIBUTE: "abc"})


class TestTheJoinWithLogs:
    def test_inside_a_span_the_ids_are_available_to_a_log_line(self, recorder: Recorder) -> None:
        with recorder.tracer.start_as_current_span("request"):
            context = current_trace_context()
        recorded = recorder.spans.get_finished_spans()[0]
        assert recorded.context is not None
        assert context["trace_id"] == trace.format_trace_id(recorded.context.trace_id)
        assert context["span_id"] == trace.format_span_id(recorded.context.span_id)

    def test_the_ids_are_hex_of_the_lengths_a_backend_expects(self, recorder: Recorder) -> None:
        # 32 and 16 hex characters. A backend that cannot parse them cannot link
        # a log line to its trace, which is the only reason they are logged.
        with recorder.tracer.start_as_current_span("request"):
            context = current_trace_context()
        assert len(context["trace_id"]) == 32
        assert len(context["span_id"]) == 16
        int(context["trace_id"], 16)
        int(context["span_id"], 16)

    def test_outside_a_span_there_is_nothing_to_add(self) -> None:
        # So logs are not padded with zeroes in a deployment that does not trace.
        assert current_trace_context() == {}


class TestWhenTracingIsOff:
    """The default path, and the one that must not cost anything."""

    def test_the_platform_tracer_still_answers(self) -> None:
        # A span from the API's no-op satisfies the same interface and simply
        # does not record, which is what lets every call site stay unconditional.
        with get_tracer().start_as_current_span("chat") as span:
            assert span.is_recording() is False

    def test_installing_nothing_leaves_the_no_op_in_place(self) -> None:
        before = trace.get_tracer_provider()
        install_tracer_provider(None)
        assert trace.get_tracer_provider() is before

    def test_shutting_down_nothing_is_not_an_error(self) -> None:
        # The lifespan calls this unconditionally: a shutdown path with a branch
        # in it is a shutdown path with an untested branch in it.
        shutdown_tracer_provider(None)
