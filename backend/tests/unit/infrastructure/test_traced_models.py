"""The tracing decorators, over the real fakes and a real SDK.

The wrappers are exercised through the same reference implementations the
contract suite runs, so a wrapper that changed a model's behaviour would fail
here for the same reason it would fail there.
"""

from collections.abc import Iterator, Sequence
from dataclasses import dataclass

import pytest
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode

from paimon.agents.tools import SEARCH_CORPUS, TOOLS
from paimon.config import Settings
from paimon.domain.errors import GenerationError
from paimon.domain.ports import (
    ChatModel,
    Completion,
    Message,
    ToolCall,
    ToolCallingChatModel,
)
from paimon.infrastructure.observability import (
    TracedChatModel,
    TracedEmbeddingModel,
    TracedToolCallingChatModel,
    trace_chat_model,
    trace_embedding_model,
)
from paimon.interfaces.api.dependencies import _build_chat_model, _build_embedding_model
from paimon.observability import genai
from paimon.observability.genai import Operation, Provider
from tests.fakes import FakeChatModel, FakeEmbeddingModel, FakeToolCallingChatModel

DIMENSIONS = 64
QUESTION = (Message(role="user", content="how do I drain a node"),)


@dataclass(frozen=True, slots=True)
class Spans:
    """Spans recorded during a test."""

    exporter: InMemorySpanExporter

    def only(self) -> ReadableSpan:
        recorded = self.exporter.get_finished_spans()
        assert len(recorded) == 1, f"expected one span, got {[s.name for s in recorded]}"
        return recorded[0]

    def attributes(self) -> dict[str, object]:
        return dict(self.only().attributes or {})


@pytest.fixture(autouse=True)
def spans(monkeypatch: pytest.MonkeyPatch) -> Iterator[Spans]:
    """Record into memory by replacing the tracer the helpers resolve.

    The process-wide provider can be installed once and never replaced, so a test
    that set it would decide for every test after it. Substituting the lookup
    keeps the decision inside this module.
    """
    memory = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(memory))
    monkeypatch.setattr(genai, "get_tracer", lambda: provider.get_tracer("paimon"))
    yield Spans(exporter=memory)
    provider.shutdown()


class TestAChatCall:
    async def test_the_span_is_named_by_operation_and_model(self, spans: Spans) -> None:
        # The convention prescribes "{operation} {model}". Not the URL, which
        # would give a dashboard one row per endpoint, and not a constant, which
        # would give it one row for everything.
        model = TracedChatModel(FakeChatModel(model_id="gpt-4o-mini"), Provider.OPENAI)
        await model.complete(QUESTION)
        assert spans.only().name == "chat gpt-4o-mini"

    async def test_it_carries_the_required_attributes(self, spans: Spans) -> None:
        model = TracedChatModel(FakeChatModel(model_id="gpt-4o-mini"), Provider.OPENAI)
        await model.complete(QUESTION)
        attributes = spans.attributes()
        assert attributes[genai.OPERATION] == Operation.CHAT.value
        assert attributes[genai.PROVIDER] == Provider.OPENAI.value
        assert attributes[genai.REQUEST_MODEL] == "gpt-4o-mini"

    async def test_it_is_a_client_span(self, spans: Spans) -> None:
        # A backend uses the kind to decide what depends on what. A client call
        # mislabelled INTERNAL vanishes from the picture of where time went.
        model = TracedChatModel(FakeChatModel(), Provider.OPENAI)
        await model.complete(QUESTION)
        assert spans.only().kind is SpanKind.CLIENT

    async def test_it_records_what_the_call_cost(self, spans: Spans) -> None:
        model = TracedChatModel(FakeChatModel(answer="two words"), Provider.OPENAI)
        completion = await model.complete(QUESTION)
        attributes = spans.attributes()
        assert attributes[genai.INPUT_TOKENS] == completion.input_tokens
        assert attributes[genai.OUTPUT_TOKENS] == completion.output_tokens

    async def test_the_requested_and_returned_models_are_both_recorded(self, spans: Spans) -> None:
        # They differ in ways that matter: an alias resolves to a dated build,
        # and a deployment name is not a model name at all. A regression that
        # arrives without a deployment is the provider moving the alias, and only
        # the response model shows it.
        model = TracedChatModel(FakeChatModel(model_id="fake-chat-v1"), Provider.OPENAI)
        await model.complete(QUESTION)
        attributes = spans.attributes()
        assert attributes[genai.REQUEST_MODEL] == "fake-chat-v1"
        assert attributes[genai.RESPONSE_MODEL] == "fake-chat-v1"

    async def test_the_sampling_parameters_are_recorded_when_given(self, spans: Spans) -> None:
        model = TracedChatModel(FakeChatModel(), Provider.OPENAI)
        await model.complete(QUESTION, temperature=0.7, max_output_tokens=256)
        attributes = spans.attributes()
        assert attributes[genai.REQUEST_TEMPERATURE] == 0.7
        assert attributes[genai.REQUEST_MAX_TOKENS] == 256


class TestFailure:
    async def test_a_provider_failure_marks_the_span_and_still_raises(self, spans: Spans) -> None:
        # Tracing observes; it does not swallow. A wrapper that turned an error
        # into a recorded span and a None would be worse than no tracing at all.
        model = TracedChatModel(_FailingChatModel(), Provider.OPENAI)
        with pytest.raises(GenerationError):
            await model.complete(QUESTION)
        recorded = spans.only()
        assert recorded.status.status_code is StatusCode.ERROR
        assert (recorded.attributes or {})["error.type"] == "GenerationError"


class TestContentCapture:
    async def test_content_is_absent_by_default(self, spans: Spans) -> None:
        # The conventions mark it opt-in and warn it carries sensitive data. Here
        # that is an organization's documentation and what its people typed.
        model = TracedChatModel(FakeChatModel(answer="cordon the node"), Provider.OPENAI)
        await model.complete(QUESTION)
        attributes = spans.attributes()
        assert genai.INPUT_MESSAGES not in attributes
        assert genai.OUTPUT_MESSAGES not in attributes

    async def test_content_is_recorded_when_asked_for(self, spans: Spans) -> None:
        model = TracedChatModel(
            FakeChatModel(answer="cordon the node"), Provider.OPENAI, capture_content=True
        )
        await model.complete(QUESTION)
        attributes = spans.attributes()
        assert "how do I drain a node" in str(attributes[genai.INPUT_MESSAGES])
        assert attributes[genai.OUTPUT_MESSAGES] == "cordon the node"


class TestTheCapabilitySurvives:
    """The hazard this wrapper exists to avoid.

    ``ToolCallingChatModel`` is checked with ``isinstance``. A wrapper that
    implemented only ``ChatModel`` would remove the capability from an adapter
    that had it, and nothing would raise — agents would simply stop being offered
    tools, everywhere, quietly.
    """

    def test_wrapping_a_tool_calling_model_keeps_it_tool_calling(self) -> None:
        wrapped = trace_chat_model(FakeToolCallingChatModel(), Provider.OPENAI)
        assert isinstance(wrapped, ToolCallingChatModel)
        assert isinstance(wrapped, TracedToolCallingChatModel)

    def test_wrapping_a_plain_model_does_not_invent_the_capability(self) -> None:
        wrapped = trace_chat_model(FakeChatModel(), Provider.OPENAI)
        assert isinstance(wrapped, ChatModel)
        assert not isinstance(wrapped, ToolCallingChatModel)

    async def test_a_tool_call_records_which_tools_were_offered_and_chosen(
        self, spans: Spans
    ) -> None:
        # The count, because definitions are sent on every turn and are therefore
        # input tokens paid again each time — the usual reason a conversation's
        # cost climbs with nothing else on the span to explain it. And the names
        # of what was chosen, because that is the shape of the model's reasoning;
        # the arguments are the caller's data and stay off.
        inner = FakeToolCallingChatModel(
            tool_calls=[
                ToolCall(call_id="1", name=SEARCH_CORPUS.name, arguments={"query": "drain"})
            ]
        )
        wrapped = trace_chat_model(inner, Provider.OPENAI)
        assert isinstance(wrapped, ToolCallingChatModel)
        await wrapped.complete_with_tools(QUESTION, TOOLS)
        attributes = spans.attributes()
        assert attributes[genai.TOOL_COUNT] == len(TOOLS)
        assert attributes[genai.TOOL_CALLS] == (SEARCH_CORPUS.name,)


class TestEmbeddings:
    async def test_embedding_documents_records_one_span(self, spans: Spans) -> None:
        model = trace_embedding_model(FakeEmbeddingModel(dimensions=DIMENSIONS), Provider.OPENAI)
        await model.embed_documents(["one", "two", "three"])
        recorded = spans.only()
        assert recorded.name.startswith(Operation.EMBEDDINGS.value)
        attributes = dict(recorded.attributes or {})
        assert attributes[genai.INPUT_COUNT] == 3
        assert attributes[genai.EMBEDDINGS_RETURNED] == 3

    async def test_a_query_is_one_input(self, spans: Spans) -> None:
        model = trace_embedding_model(FakeEmbeddingModel(dimensions=DIMENSIONS), Provider.OPENAI)
        await model.embed_query("how do I drain a node")
        assert spans.attributes()[genai.INPUT_COUNT] == 1

    async def test_the_text_being_embedded_is_never_recorded(self, spans: Spans) -> None:
        # What is embedded during ingestion is the corpus itself. Recording it
        # would copy an organization's documentation into a tracing backend one
        # chunk at a time, so there is no switch for this one.
        model = trace_embedding_model(FakeEmbeddingModel(dimensions=DIMENSIONS), Provider.OPENAI)
        await model.embed_documents(["a secret runbook"])
        assert "secret runbook" not in str(spans.attributes())

    async def test_the_wrapper_reports_the_model_it_wraps(self) -> None:
        inner = FakeEmbeddingModel(dimensions=DIMENSIONS)
        wrapped = trace_embedding_model(inner, Provider.OPENAI)
        assert wrapped.model_id == inner.model_id
        assert wrapped.dimensions == inner.dimensions


class _FailingChatModel:
    """A provider that does not answer."""

    @property
    def model_id(self) -> str:
        return "broken-v1"

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        temperature: float = 0.0,
        max_output_tokens: int | None = None,
    ) -> Completion:
        msg = "the provider did not answer"
        raise GenerationError(msg)


class TestTheCompositionRootActuallyWrapsThem:
    """The wiring, not the wrapper.

    Every test above builds its own wrapper, which is what makes them fast — and
    what would let the composition root stop wrapping without a single failure.
    That has happened once already in this project, in Phase 4, so it gets a test
    of its own here.
    """

    def test_the_chat_model_the_platform_builds_is_traced(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert isinstance(_build_chat_model(_settings(monkeypatch)), TracedChatModel)

    def test_the_embedding_model_the_platform_builds_is_traced(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert isinstance(_build_embedding_model(_settings(monkeypatch)), TracedEmbeddingModel)

    def test_a_local_endpoint_is_named_as_the_openai_dialect(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The attribute names the wire format, not the company: this adapter
        # speaks the OpenAI format at whatever endpoint it is pointed at, and the
        # registry has no value for "something OpenAI-compatible".
        model = _build_chat_model(_settings(monkeypatch))
        assert isinstance(model, TracedChatModel)
        assert model._provider is Provider.OPENAI


def _settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Minimal settings for a local deployment."""
    for key, value in {
        "PAIMON_ENVIRONMENT": "local",
        "PAIMON_DATABASE__HOST": "localhost",
        "PAIMON_DATABASE__USER": "paimon",
        "PAIMON_DATABASE__PASSWORD": "s3cret",
        "PAIMON_DATABASE__NAME": "paimon",
        "PAIMON_REDIS__HOST": "localhost",
        "PAIMON_AUTH__PROVIDER": "dev",
        "PAIMON_AUTH__DEV_SIGNING_KEY": "local-only-padded-to-thirty-two-bytes",
    }.items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=None)
