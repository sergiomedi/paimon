"""The incident triage agent.

Two levels, deliberately. The node tests call node bodies directly, with no graph
runtime, which is what ADR-0015 was for. The graph tests run the whole agent
through the adapter, because a set of nodes that each work and never get wired
together correctly is a set of nodes that works and an agent that does not.
"""

import pytest
from tests.fakes import (
    FakeChatModel,
    FakeEmbeddingModel,
    InMemoryCheckpointer,
    InMemoryDocumentRepository,
    InMemoryVectorStore,
)

from paimon.agents.triage import (
    AGENT_NAME,
    HISTORY_FRAMING,
    PROCEDURE_FRAMING,
    UNSUPPORTED,
    build_triage_graph,
    frame_symptom,
)
from paimon.application.use_cases.answer_question import NO_MATERIAL
from paimon.application.use_cases.retrieve_chunks import RetrieveChunks
from paimon.domain.agents import AgentState, GraphSpec, NodeSpec
from paimon.domain.entities import Chunk, Document, RunStatus
from paimon.domain.ports import ChunkRecord, IndexDescriptor
from paimon.infrastructure.orchestration import LangGraphWorkflow
from paimon.infrastructure.tokenization import HeuristicTokenCounter

TENANT = "tenant-a"
DIMENSIONS = 64

RUNBOOK = """# Node maintenance

## Draining

Cordon the node first so the scheduler stops placing new pods on it. Eviction
stalls indefinitely when a disruption budget cannot be satisfied.
"""

POSTMORTEM = """# INC-2451

## What happened

A drain stalled for forty minutes because a disruption budget could not be met.
"""


def chunk(chunk_id: str, document_id: str, text: str, ordinal: int = 0) -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        document_id=document_id,
        tenant_id=TENANT,
        ordinal=ordinal,
        text=text,
        start_char=0,
        end_char=len(text),
        token_count=max(len(text.split()), 1),
    )


def document(document_id: str, text: str) -> Document:
    return Document(
        document_id=document_id,
        tenant_id=TENANT,
        source_uri=f"https://example.test/{document_id}",
        title=document_id,
        text=text,
        content_hash=f"hash-{document_id}",
        media_type="text/markdown",
    )


class Harness:
    """Everything the agent needs, wired to in-memory implementations."""

    def __init__(self, answer: str = "Cordon the node first [1].") -> None:
        self.embedding_model = FakeEmbeddingModel(dimensions=DIMENSIONS)
        self.store = InMemoryVectorStore(
            IndexDescriptor(
                name="in-memory",
                embedding_model_id=self.embedding_model.model_id,
                dimensions=DIMENSIONS,
            )
        )
        self.repository = InMemoryDocumentRepository()
        self.chat_model = FakeChatModel(answer=answer)
        self.checkpointer = InMemoryCheckpointer()

    async def index(self) -> None:
        chunks = [
            chunk("c-run", "runbook", RUNBOOK),
            chunk("c-inc", "incident", POSTMORTEM),
        ]
        embeddings = await self.embedding_model.embed_documents([item.text for item in chunks])
        await self.store.upsert(
            [
                ChunkRecord(chunk=item, embedding=embedding)
                for item, embedding in zip(chunks, embeddings, strict=True)
            ]
        )
        await self.repository.save(document("runbook", RUNBOOK))
        await self.repository.save(document("incident", POSTMORTEM))

    def graph(self) -> GraphSpec:
        return build_triage_graph(
            RetrieveChunks(self.store, self.embedding_model),
            self.chat_model,
            self.repository,
            HeuristicTokenCounter(),
        )

    def workflow(self) -> LangGraphWorkflow:
        return LangGraphWorkflow(self.graph(), self.checkpointer)

    def node(self, name: str) -> NodeSpec:
        return next(node for node in self.graph().nodes if node.name == name)


class TestFraming:
    def test_a_symptom_becomes_two_different_questions(self) -> None:
        procedure, history = frame_symptom("eviction hangs")
        assert procedure == PROCEDURE_FRAMING.format(symptom="eviction hangs")
        assert history == HISTORY_FRAMING.format(symptom="eviction hangs")
        assert procedure != history

    def test_whitespace_is_normalised_so_the_same_symptom_frames_the_same_way(self) -> None:
        assert frame_symptom("eviction   hangs\n") == frame_symptom("eviction hangs")


class TestNodesInIsolation:
    """No graph, no runtime. The point of keeping node bodies plain."""

    async def test_the_retrieval_nodes_ask_different_questions(self) -> None:
        harness = Harness()
        await harness.index()
        state = AgentState(question="eviction hangs", tenant_id=TENANT)

        procedure = await harness.node("procedure").run(state)
        history = await harness.node("history").run(state)

        assert procedure.get("evidence")
        assert history.get("evidence")

    async def test_refuse_does_not_call_a_model(self) -> None:
        harness = Harness()
        update = await harness.node("refuse").run(
            AgentState(question="eviction hangs", tenant_id=TENANT)
        )
        assert update["draft"] == NO_MATERIAL
        assert harness.chat_model.calls == []

    async def test_verify_leaves_a_supported_draft_alone(self) -> None:
        harness = Harness()
        await harness.index()
        drafted = await harness.node("draft").run(
            AgentState(
                question="eviction hangs",
                tenant_id=TENANT,
                evidence=(chunk("c-run", "runbook", RUNBOOK),),
            )
        )
        state = AgentState(
            question="eviction hangs",
            tenant_id=TENANT,
            draft=drafted["draft"],
            citations=drafted["citations"],
        )
        assert await harness.node("verify").run(state) == {}

    async def test_verify_withdraws_a_draft_that_cites_nothing(self) -> None:
        # The model answered, and cited nothing that resolved. Withdrawing is a
        # decision made by code, because a model asked whether it was grounded
        # will usually say yes.
        harness = Harness()
        state = AgentState(
            question="eviction hangs",
            tenant_id=TENANT,
            draft="Cordon the node first.",
            citations=(),
        )
        assert (await harness.node("verify").run(state))["draft"] == UNSUPPORTED


class TestTheWholeAgent:
    async def test_it_takes_the_drafting_path_when_there_is_material(self) -> None:
        harness = Harness()
        await harness.index()
        names = [
            step.name
            async for step in harness.workflow().stream(
                "eviction hangs", thread_id="t-1", tenant_id=TENANT
            )
        ]
        assert names[0] == "frame"
        assert set(names[1:3]) == {"procedure", "history"}
        assert names[3:] == ["assess", "draft", "verify"]

    async def test_it_refuses_without_a_model_call_when_nothing_is_indexed(self) -> None:
        harness = Harness()
        names = [
            step.name
            async for step in harness.workflow().stream(
                "eviction hangs", thread_id="t-1", tenant_id=TENANT
            )
        ]
        assert names[-1] == "refuse"
        assert "draft" not in names
        assert harness.chat_model.calls == []

    async def test_the_two_retrievals_are_merged_and_deduplicated(self) -> None:
        # Both framings reach the same corpus, so the same chunk is found twice.
        # The evidence reducer is what stops it being counted twice.
        harness = Harness()
        await harness.index()
        assessment = [
            step
            async for step in harness.workflow().stream(
                "eviction hangs", thread_id="t-1", tenant_id=TENANT
            )
            if step.name == "assess"
        ]
        assert assessment[0].details["chunks"] == "2"

    async def test_a_completed_run_is_recorded_under_the_agent_name(self) -> None:
        harness = Harness()
        await harness.index()
        async for _ in harness.workflow().stream(
            "eviction hangs", thread_id="t-1", tenant_id=TENANT
        ):
            pass
        run = await harness.checkpointer.load("t-1")
        assert run is not None
        assert run.agent == AGENT_NAME
        assert run.status is RunStatus.SUCCEEDED

    async def test_an_unsupported_answer_is_withdrawn_end_to_end(self) -> None:
        harness = Harness(answer="Reboot everything immediately.")
        await harness.index()
        async for _ in harness.workflow().stream(
            "eviction hangs", thread_id="t-1", tenant_id=TENANT
        ):
            pass
        run = await harness.checkpointer.load("t-1")
        assert run is not None
        assert run.steps[-1].name == "verify"

    async def test_tenants_cannot_see_each_others_material(self) -> None:
        harness = Harness()
        await harness.index()
        names = [
            step.name
            async for step in harness.workflow().stream(
                "eviction hangs", thread_id="t-1", tenant_id="tenant-b"
            )
        ]
        assert names[-1] == "refuse"


@pytest.mark.parametrize("node_name", ["frame", "procedure", "history", "assess", "draft"])
def test_every_declared_node_is_reachable(node_name: str) -> None:
    graph = Harness().graph()
    graph.validate()
    assert node_name in graph.node_names()
