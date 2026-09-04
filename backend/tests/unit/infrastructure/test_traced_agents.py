"""Agent spans, over a real compiled graph.

The workflow adapter already timed every node before this batch; these tests are
about the shape of what is exported — one span for the run, one per node, the
node spans inside the run's — because a trace whose nesting is wrong is a trace
that has to be read as a list.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from typing import cast

import pytest
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from paimon.agents.triage import AGENT_NAME as TRIAGE
from paimon.domain.errors import EmbeddingError
from paimon.observability import genai
from tests.e2e.test_agents_api import TENANT, Backend
from tests.fakes import FakeEmbeddingModel

QUESTION = "eviction hangs"


@dataclass(frozen=True, slots=True)
class Spans:
    exporter: InMemorySpanExporter

    def all(self) -> list[ReadableSpan]:
        return list(self.exporter.get_finished_spans())

    def named(self, name: str) -> list[ReadableSpan]:
        return [span for span in self.all() if span.name == name]


@pytest.fixture(autouse=True)
def spans(monkeypatch: pytest.MonkeyPatch) -> Iterator[Spans]:
    memory = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(memory))
    monkeypatch.setattr(genai, "get_tracer", lambda: provider.get_tracer("paimon"))
    yield Spans(exporter=memory)
    provider.shutdown()


async def run_triage(backend: Backend) -> None:
    workflow = backend.workflows()[TRIAGE]
    async for _ in workflow.stream(QUESTION, thread_id="t-1", tenant_id=TENANT):
        pass


class TestARun:
    async def test_the_run_is_one_span_with_the_agent_s_name(self, spans: Spans) -> None:
        backend = Backend()
        await backend.index()
        await run_triage(backend)
        assert len(spans.named(f"invoke_agent {TRIAGE}")) == 1

    async def test_the_run_span_carries_the_thread_as_its_conversation(self, spans: Spans) -> None:
        # The thread id is what joins a run to the trace of its resumption, which
        # is a separate request minutes or hours later.
        backend = Backend()
        await backend.index()
        await run_triage(backend)
        attributes = dict(spans.named(f"invoke_agent {TRIAGE}")[0].attributes or {})
        assert attributes[genai.AGENT_NAME] == TRIAGE
        assert attributes[genai.CONVERSATION_ID] == "t-1"

    async def test_every_node_that_ran_has_a_span(self, spans: Spans) -> None:
        backend = Backend()
        await backend.index()
        await run_triage(backend)
        nodes = {
            (span.attributes or {}).get(genai.AGENT_NODE)
            for span in spans.all()
            if genai.AGENT_NODE in (span.attributes or {})
        }
        # The triage graph frames, retrieves twice, assesses and drafts.
        assert {"frame", "procedure", "history", "assess", "draft"} <= nodes

    async def test_the_node_spans_belong_to_the_run_span(self, spans: Spans) -> None:
        # Nesting, not adjacency. A flat list of node spans would make a reader
        # reconstruct which run each belonged to, which is the work a trace
        # exists to have already done.
        backend = Backend()
        await backend.index()
        await run_triage(backend)
        run = spans.named(f"invoke_agent {TRIAGE}")[0]
        assert run.context is not None
        nodes = [span for span in spans.all() if genai.AGENT_NODE in (span.attributes or {})]
        assert nodes
        for node in nodes:
            assert node.parent is not None
            assert node.parent.trace_id == run.context.trace_id

    async def test_a_node_records_what_it_spent(self, spans: Spans) -> None:
        # The same number the run record carries, on the span. A step that cost
        # tokens and a step that did not look identical in a timeline otherwise.
        backend = Backend()
        await backend.index()
        await run_triage(backend)
        spent = [
            (span.attributes or {}).get(genai.AGENT_STEP_TOKENS)
            for span in spans.all()
            if genai.AGENT_STEP_TOKENS in (span.attributes or {})
        ]
        assert spent
        assert any(isinstance(value, int) and value > 0 for value in spent)


class TestAFailingNode:
    async def test_a_node_that_fails_marks_its_span(self, spans: Spans) -> None:
        # The adapter catches a node failure and turns it into a recorded step
        # rather than raising, so without this the trace would show a node that
        # took some time and succeeded — the opposite of what happened.
        backend = Backend()
        # Nothing indexed and an unreachable embedding model: retrieval fails.
        # Assigned through the attribute the Backend exposes, which is typed for
        # the fake; the substitute satisfies the port, which is what matters.
        backend.embedding_model = cast("FakeEmbeddingModel", _UnreachableEmbeddingModel())
        await run_triage(backend)
        failed = [span for span in spans.all() if span.status.status_code.name == "ERROR"]
        assert failed


class _UnreachableEmbeddingModel:
    """An embedding model whose provider is down."""

    @property
    def model_id(self) -> str:
        return "unreachable-v1"

    @property
    def dimensions(self) -> int:
        return 64

    async def embed_documents(self, texts: object) -> list[object]:
        msg = "the embedding provider is unreachable"
        raise EmbeddingError(msg)

    async def embed_query(self, text: str) -> object:
        msg = "the embedding provider is unreachable"
        raise EmbeddingError(msg)
