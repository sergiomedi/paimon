"""End-to-end tests for the agent endpoints.

Through FastAPI rather than by calling the router functions: the streaming
response, the dependency wiring and the JSON shapes are the parts most likely to
be wrong, and none of them exist until the application runs. The workflows behind
the endpoints are wired to reference implementations, so the whole HTTP path is
exercised without a database or a model server.
"""

import json
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from paimon.agents import AGENTS
from paimon.agents.triage import AGENT_NAME as TRIAGE
from paimon.application.use_cases import RetrieveChunks
from paimon.domain.entities import Chunk, Document
from paimon.domain.ports import AgentCheckpointer, AgentWorkflow, ChunkRecord, IndexDescriptor
from paimon.infrastructure.identity import DevIdentityProvider
from paimon.infrastructure.orchestration import LangGraphWorkflow
from paimon.infrastructure.tokenization import HeuristicTokenCounter
from paimon.interfaces.api.dependencies import get_agent_workflows, get_checkpointer
from tests.fakes import (
    FakeChatModel,
    FakeEmbeddingModel,
    InMemoryCheckpointer,
    InMemoryDocumentRepository,
    InMemoryVectorStore,
)

TENANT = "tenant-1"
DIMENSIONS = 64
RUNBOOK = """# Node maintenance

Cordon the node first so the scheduler stops placing new pods on it. Eviction
stalls indefinitely when a disruption budget cannot be satisfied.
"""


class Backend:
    """Every agent, wired to reference implementations."""

    def __init__(self, answer: str = "Cordon the node first [1].") -> None:
        self.embedding_model = FakeEmbeddingModel(dimensions=DIMENSIONS)
        self.chat_model = FakeChatModel(answer=answer)
        self.repository = InMemoryDocumentRepository()
        self.checkpointer = InMemoryCheckpointer()
        self.store = InMemoryVectorStore(
            IndexDescriptor(
                name="test",
                embedding_model_id=self.embedding_model.model_id,
                dimensions=DIMENSIONS,
            )
        )

    async def index(self) -> None:
        chunk = Chunk(
            chunk_id="c-1",
            document_id="runbook",
            tenant_id=TENANT,
            ordinal=0,
            text=RUNBOOK,
            start_char=0,
            end_char=len(RUNBOOK),
            token_count=len(RUNBOOK.split()),
        )
        embeddings = await self.embedding_model.embed_documents([chunk.text])
        await self.store.upsert([ChunkRecord(chunk=chunk, embedding=embeddings[0])])
        await self.repository.save(
            Document(
                document_id="runbook",
                tenant_id=TENANT,
                source_uri="https://example.test/runbook.md",
                title="Node maintenance",
                text=RUNBOOK,
                content_hash="hash",
                media_type="text/markdown",
            )
        )

    def workflows(self) -> dict[str, AgentWorkflow]:
        retrieve = RetrieveChunks(self.store, self.embedding_model)
        return {
            name: LangGraphWorkflow(
                builder(retrieve, self.chat_model, self.repository, HeuristicTokenCounter()),
                self.checkpointer,
            )
            for name, builder in AGENTS.items()
        }

    def runs(self) -> AgentCheckpointer:
        return self.checkpointer


@pytest.fixture
def backend(app: FastAPI) -> Iterator[Backend]:
    instance = Backend()
    app.dependency_overrides[get_agent_workflows] = instance.workflows
    app.dependency_overrides[get_checkpointer] = instance.runs
    yield instance
    app.dependency_overrides.clear()


@pytest.fixture
def auth(dev_identity_provider: DevIdentityProvider) -> dict[str, str]:
    token = dev_identity_provider.issue(subject="user-1", tenant_id=TENANT)
    return {"Authorization": f"Bearer {token}"}


def records(body: str) -> list[dict[str, object]]:
    """Parse an NDJSON stream into the records it carries."""
    return [json.loads(line) for line in body.splitlines() if line.strip()]


class TestListingAgents:
    async def test_every_registered_agent_is_offered(
        self, client: AsyncClient, backend: Backend, auth: dict[str, str]
    ) -> None:
        response = await client.get("/api/v1/agents", headers=auth)
        assert response.status_code == 200
        assert {item["name"] for item in response.json()} == set(AGENTS)

    async def test_each_one_says_what_it_does(
        self, client: AsyncClient, backend: Backend, auth: dict[str, str]
    ) -> None:
        response = await client.get("/api/v1/agents", headers=auth)
        assert all(item["description"] for item in response.json())

    async def test_listing_requires_authentication(self, client: AsyncClient) -> None:
        assert (await client.get("/api/v1/agents")).status_code == 401


class TestStartingARun:
    async def test_the_steps_arrive_as_one_json_object_per_line(
        self, client: AsyncClient, backend: Backend, auth: dict[str, str]
    ) -> None:
        await backend.index()
        response = await client.post(
            f"/api/v1/agents/{TRIAGE}/runs", json={"input": "eviction hangs"}, headers=auth
        )

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/x-ndjson")
        names = [record["name"] for record in records(response.text)]
        assert names[0] == "frame"
        assert names[-1] == "verify"

    async def test_each_step_carries_what_a_client_needs_to_show_progress(
        self, client: AsyncClient, backend: Backend, auth: dict[str, str]
    ) -> None:
        await backend.index()
        response = await client.post(
            f"/api/v1/agents/{TRIAGE}/runs", json={"input": "eviction hangs"}, headers=auth
        )
        first = records(response.text)[0]
        assert set(first) >= {"name", "summary", "duration_ms", "started_at", "details"}

    async def test_the_thread_id_comes_back_in_a_header(
        self, client: AsyncClient, backend: Backend, auth: dict[str, str]
    ) -> None:
        # So a client that loses the stream can still read the run back: the
        # steps are checkpointed as they happen, so a dropped connection costs
        # the stream, not the record.
        await backend.index()
        response = await client.post(
            f"/api/v1/agents/{TRIAGE}/runs", json={"input": "eviction hangs"}, headers=auth
        )
        thread_id = response.headers["X-Paimon-Thread-Id"]
        stored = await client.get(f"/api/v1/agents/runs/{thread_id}", headers=auth)
        assert stored.status_code == 200
        assert stored.json()["status"] == "succeeded"

    async def test_an_unknown_agent_is_a_404_naming_the_ones_that_exist(
        self, client: AsyncClient, backend: Backend, auth: dict[str, str]
    ) -> None:
        response = await client.post(
            "/api/v1/agents/nonexistent/runs", json={"input": "anything"}, headers=auth
        )
        assert response.status_code == 404
        assert TRIAGE in response.json()["detail"]

    async def test_an_empty_input_is_rejected_before_anything_runs(
        self, client: AsyncClient, backend: Backend, auth: dict[str, str]
    ) -> None:
        response = await client.post(
            f"/api/v1/agents/{TRIAGE}/runs", json={"input": ""}, headers=auth
        )
        assert response.status_code == 422
        assert backend.chat_model.calls == []


class TestReadingRunsBack:
    async def test_a_run_reports_what_it_cost(
        self, client: AsyncClient, backend: Backend, auth: dict[str, str]
    ) -> None:
        await backend.index()
        started = await client.post(
            f"/api/v1/agents/{TRIAGE}/runs", json={"input": "eviction hangs"}, headers=auth
        )
        thread_id = started.headers["X-Paimon-Thread-Id"]
        body = (await client.get(f"/api/v1/agents/runs/{thread_id}", headers=auth)).json()
        assert body["total_tokens"] > 0
        assert body["agent"] == TRIAGE

    async def test_listing_shows_the_run_that_just_ran(
        self, client: AsyncClient, backend: Backend, auth: dict[str, str]
    ) -> None:
        await backend.index()
        await client.post(
            f"/api/v1/agents/{TRIAGE}/runs", json={"input": "eviction hangs"}, headers=auth
        )
        body = (await client.get("/api/v1/agents/runs", headers=auth)).json()
        assert [run["agent"] for run in body["runs"]] == [TRIAGE]

    async def test_another_tenants_run_is_reported_as_absent_not_forbidden(
        self, client: AsyncClient, backend: Backend, auth: dict[str, str]
    ) -> None:
        # Telling a caller that a thread exists but is not theirs is itself a
        # disclosure, and there is nothing they can do with the answer.
        await backend.index()
        started = await client.post(
            f"/api/v1/agents/{TRIAGE}/runs", json={"input": "eviction hangs"}, headers=auth
        )
        thread_id = started.headers["X-Paimon-Thread-Id"]

        other = DevIdentityProvider(
            signing_key="test-signing-key-padded-to-thirty-two-bytes"
        ).issue(subject="user-2", tenant_id="tenant-2")
        response = await client.get(
            f"/api/v1/agents/runs/{thread_id}",
            headers={"Authorization": f"Bearer {other}"},
        )
        assert response.status_code == 404

    async def test_an_unknown_run_is_absent(
        self, client: AsyncClient, backend: Backend, auth: dict[str, str]
    ) -> None:
        assert (
            await client.get("/api/v1/agents/runs/never-started", headers=auth)
        ).status_code == 404

    async def test_reading_runs_requires_authentication(self, client: AsyncClient) -> None:
        assert (await client.get("/api/v1/agents/runs")).status_code == 401
