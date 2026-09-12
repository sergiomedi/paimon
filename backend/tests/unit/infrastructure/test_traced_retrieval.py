"""Retrieval spans, and the capability that must survive being wrapped."""

from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from paimon.domain import ports
from paimon.domain.entities import Chunk
from paimon.domain.ports import (
    ChunkRecord,
    IndexDescriptor,
    ManagedIndex,
    NativeHybridSearch,
    SearchFilters,
)
from paimon.infrastructure.observability import (
    TracedHybridVectorStore,
    TracedVectorStore,
    trace_vector_store,
)
from paimon.infrastructure.observability.retrieval import STRATEGY
from paimon.observability import genai
from tests.fakes import (
    FakeEmbeddingModel,
    InMemoryHybridManagedVectorStore,
    InMemoryHybridVectorStore,
    InMemoryManagedVectorStore,
    InMemoryVectorStore,
)

#: Every optional capability that sits beside VectorStore, discovered rather than
#: listed. A capability is a protocol somewhere else chooses a code path with, so
#: the set of them is exactly the set of ways a wrapper can silently change
#: behaviour — and a list would go stale the moment somebody adds the third.
CAPABILITIES: tuple[type, ...] = tuple(
    member
    for member in vars(ports.retrieval).values()
    if isinstance(member, type)
    and getattr(member, "_is_protocol", False)
    and getattr(member, "_is_runtime_protocol", False)
    and member.__module__ == ports.retrieval.__name__
    and member is not ports.VectorStore
)


#: The same, in a stable order, so the parametrised test ids do not move about.
BY_NAME: tuple[type, ...] = tuple(sorted(CAPABILITIES, key=lambda protocol: protocol.__name__))


def protocol_attributes(protocol: type) -> set[str]:
    """The members isinstance() will look for on a runtime protocol."""
    declared = getattr(protocol, "__protocol_attrs__", None)
    if declared is not None:
        return set(declared)
    return {name for name in vars(protocol) if not name.startswith("_")}


def capable_store(descriptor: IndexDescriptor, capability: type) -> InMemoryVectorStore:
    """A store satisfying one capability, built from the protocol itself.

    Built rather than written, so that a capability added tomorrow gets a fake
    today. The stubs are never called: what is under test is whether the wrapper
    still answers to the protocol, and answering is the whole contract.
    """

    async def stub(*_args: object, **_kwargs: object) -> None:
        return None

    fake = type(
        f"Fake{capability.__name__}Store",
        (InMemoryVectorStore,),
        dict.fromkeys(protocol_attributes(capability), stub),
    )
    return fake(descriptor)  # type: ignore[no-any-return]


TENANT = "tenant-1"
DIMENSIONS = 64
QUERY = "how do I drain a node"


@dataclass(frozen=True, slots=True)
class Spans:
    exporter: InMemorySpanExporter

    def only(self) -> ReadableSpan:
        recorded = self.exporter.get_finished_spans()
        assert len(recorded) == 1, f"expected one span, got {[s.name for s in recorded]}"
        return recorded[0]

    def attributes(self) -> dict[str, object]:
        return dict(self.only().attributes or {})


@pytest.fixture(autouse=True)
def spans(monkeypatch: pytest.MonkeyPatch) -> Iterator[Spans]:
    memory = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(memory))
    monkeypatch.setattr(genai, "get_tracer", lambda: provider.get_tracer("paimon"))
    yield Spans(exporter=memory)
    provider.shutdown()


def descriptor() -> IndexDescriptor:
    return IndexDescriptor(
        name="paimon-chunks", embedding_model_id="fake-embed-v1", dimensions=DIMENSIONS
    )


async def indexed(store: InMemoryVectorStore | InMemoryHybridVectorStore) -> None:
    model = FakeEmbeddingModel(dimensions=DIMENSIONS)
    chunk = Chunk(
        chunk_id="c-1",
        document_id="runbook",
        tenant_id=TENANT,
        ordinal=0,
        text="Cordon the node first so the scheduler stops placing pods.",
        start_char=0,
        end_char=58,
        token_count=10,
    )
    embeddings = await model.embed_documents([chunk.text])
    await store.upsert([ChunkRecord(chunk=chunk, embedding=embeddings[0])])


class TestASearch:
    async def test_a_dense_search_records_a_retrieval_span(self, spans: Spans) -> None:
        store = InMemoryVectorStore(descriptor())
        await indexed(store)
        wrapped = trace_vector_store(store)
        model = FakeEmbeddingModel(dimensions=DIMENSIONS)
        await wrapped.search_dense(
            await model.embed_query(QUERY), top_k=5, filters=SearchFilters(tenant_id=TENANT)
        )
        recorded = spans.only()
        assert recorded.name == "retrieval paimon-chunks"
        attributes = dict(recorded.attributes or {})
        assert attributes[genai.OPERATION] == "retrieval"
        assert attributes[genai.DATA_SOURCE] == "paimon-chunks"
        assert attributes[genai.RETRIEVAL_TOP_K] == 5

    async def test_it_records_how_many_hits_came_back(self, spans: Spans) -> None:
        # The difference between "retrieval was slow" and "retrieval found
        # nothing" — and between "the model was wrong" and "the model was given
        # nothing to be right about".
        store = InMemoryVectorStore(descriptor())
        await indexed(store)
        wrapped = trace_vector_store(store)
        model = FakeEmbeddingModel(dimensions=DIMENSIONS)
        hits = await wrapped.search_dense(
            await model.embed_query(QUERY), top_k=5, filters=SearchFilters(tenant_id=TENANT)
        )
        assert spans.attributes()[genai.HITS_RETURNED] == len(hits)

    async def test_the_strategy_is_recorded(self, spans: Spans) -> None:
        # Which path ran is the first thing to know when a query returns
        # something surprising, and it is not recoverable from a trace otherwise.
        store = InMemoryVectorStore(descriptor())
        await indexed(store)
        wrapped = trace_vector_store(store)
        await wrapped.search_lexical(QUERY, top_k=3, filters=SearchFilters(tenant_id=TENANT))
        assert spans.attributes()[STRATEGY] == "lexical"

    async def test_the_query_text_is_absent_by_default(self, spans: Spans) -> None:
        # A query is what somebody asked. Opt-in in the conventions for exactly
        # that reason.
        store = InMemoryVectorStore(descriptor())
        await indexed(store)
        wrapped = trace_vector_store(store)
        await wrapped.search_lexical(QUERY, top_k=3, filters=SearchFilters(tenant_id=TENANT))
        assert genai.RETRIEVAL_QUERY not in spans.attributes()

    async def test_the_query_text_is_recorded_when_asked_for(self, spans: Spans) -> None:
        store = InMemoryVectorStore(descriptor())
        await indexed(store)
        wrapped = trace_vector_store(store, capture_content=True)
        await wrapped.search_lexical(QUERY, top_k=3, filters=SearchFilters(tenant_id=TENANT))
        assert spans.attributes()[genai.RETRIEVAL_QUERY] == QUERY


class TestWritesAreNotTraced:
    async def test_an_upsert_records_nothing(self, spans: Spans) -> None:
        # Ingestion's cost is the embedding call, which has its own span, and the
        # write is a statement the database instrumentation already records.
        store = InMemoryVectorStore(descriptor())
        wrapped = trace_vector_store(store)
        model = FakeEmbeddingModel(dimensions=DIMENSIONS)
        chunk = Chunk(
            chunk_id="c-2",
            document_id="runbook",
            tenant_id=TENANT,
            ordinal=0,
            text="text",
            start_char=0,
            end_char=4,
            token_count=1,
        )
        embeddings = await model.embed_documents([chunk.text])
        await wrapped.upsert([ChunkRecord(chunk=chunk, embedding=embeddings[0])])
        assert spans.exporter.get_finished_spans() == ()


class TestTheCapabilitySurvives:
    """The second time this hazard has appeared, and the more expensive one.

    Losing ``NativeHybridSearch`` raises nothing. Azure AI Search would quietly
    stop using its own fusion and start being fused in-process — a change in
    retrieval *quality*, invisible in every log and every test that does not
    check for exactly this.
    """

    def test_wrapping_a_hybrid_store_keeps_it_hybrid(self) -> None:
        wrapped = trace_vector_store(InMemoryHybridVectorStore(descriptor()))
        assert isinstance(wrapped, NativeHybridSearch)
        assert isinstance(wrapped, TracedHybridVectorStore)

    def test_wrapping_a_plain_store_does_not_invent_the_capability(self) -> None:
        wrapped = trace_vector_store(InMemoryVectorStore(descriptor()))
        assert isinstance(wrapped, TracedVectorStore)
        assert not isinstance(wrapped, NativeHybridSearch)

    async def test_a_native_hybrid_search_is_labelled_as_one(self, spans: Spans) -> None:
        store = InMemoryHybridVectorStore(descriptor())
        await indexed(store)
        wrapped = trace_vector_store(store)
        assert isinstance(wrapped, NativeHybridSearch)
        model = FakeEmbeddingModel(dimensions=DIMENSIONS)
        await wrapped.search_hybrid(
            QUERY,
            await model.embed_query(QUERY),
            top_k=4,
            filters=SearchFilters(tenant_id=TENANT),
        )
        assert spans.attributes()[STRATEGY] == "native_hybrid"

    def test_wrapping_a_managed_store_keeps_the_index_creatable(self) -> None:
        """The second capability, added in Phase 7 and immediately lost.

        ManagedIndex went on the port and this factory did not learn about it, so
        the benchmark whose entire purpose is to exercise Azure AI Search was told
        by Azure AI Search that it had no index to create. Silent in exactly the
        way the class above describes.
        """
        wrapped = trace_vector_store(InMemoryManagedVectorStore(descriptor()))
        assert isinstance(wrapped, ManagedIndex)

    async def test_creating_the_index_reaches_the_wrapped_store(self) -> None:
        store = InMemoryManagedVectorStore(descriptor())
        wrapped = trace_vector_store(store)
        assert isinstance(wrapped, ManagedIndex)

        await wrapped.ensure_index()

        assert store.ensured == 1

    def test_both_capabilities_survive_together(self) -> None:
        """Which is what Azure AI Search is: native fusion and a managed index."""
        wrapped = trace_vector_store(InMemoryHybridManagedVectorStore(descriptor()))
        assert isinstance(wrapped, NativeHybridSearch)
        assert isinstance(wrapped, ManagedIndex)

    @pytest.mark.parametrize("capability", BY_NAME)
    def test_no_capability_is_lost_by_wrapping(self, capability: type) -> None:
        """Enumerated rather than listed, so the *next* capability is covered too.

        Every optional protocol beside VectorStore is discovered from the ports
        module and checked here. A third capability added without teaching
        trace_vector_store about it fails this test on the day it is written,
        rather than in a deployed environment three phases later.
        """
        store = capable_store(descriptor(), capability)
        assert isinstance(store, capability), "the fake must satisfy what it claims"

        wrapped = trace_vector_store(store)

        assert isinstance(wrapped, capability), (
            f"{capability.__name__} did not survive being wrapped. Every capability is "
            f"chosen by an isinstance check somewhere, so losing one is silent."
        )

    def test_the_wrapper_reports_the_index_it_wraps(self) -> None:
        wrapped = trace_vector_store(InMemoryVectorStore(descriptor()))
        assert wrapped.descriptor == descriptor()
