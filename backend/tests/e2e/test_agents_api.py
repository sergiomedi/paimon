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
from langgraph.checkpoint.memory import InMemorySaver

from paimon.agents import AGENTS, AgentCollaborators, build_all
from paimon.agents.triage import AGENT_NAME as TRIAGE
from paimon.application.use_cases import RetrieveChunks
from paimon.domain.entities import Chunk, Document
from paimon.domain.ports import AgentCheckpointer, AgentWorkflow, ChunkRecord, IndexDescriptor
from paimon.infrastructure.identity import DevIdentityProvider
from paimon.infrastructure.orchestration import LangGraphWorkflow, build_serializer
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

    def __init__(self, answer: str = "Cordon the node first [1].", *, review: bool = False) -> None:
        self.review = review
        self._workflows: dict[str, AgentWorkflow] | None = None
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
        """Build once and hand back the same instances.

        FastAPI calls a dependency override on every request, so building here
        would give each request a fresh graph checkpointer — and a run suspended
        by one request would be unknown to the next. The application builds these
        once at startup; the override has to mirror that or it tests a lifecycle
        the platform does not have.
        """
        if self._workflows is not None:
            return self._workflows
        collaborators = AgentCollaborators(
            retrieve=RetrieveChunks(self.store, self.embedding_model),
            chat_model=self.chat_model,
            repository=self.repository,
            token_counter=HeuristicTokenCounter(),
        )
        self._workflows = {
            name: LangGraphWorkflow(
                spec, self.checkpointer, saver=InMemorySaver(serde=build_serializer())
            )
            for name, spec in build_all(collaborators, review_postmortems=self.review).items()
        }
        return self._workflows

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


class TestReviewingASuspendedRun:
    """A postmortem is the one output an organization publishes, so it is the
    one agent where waiting for a person is worth it when a deployment asks."""

    @pytest.fixture
    def reviewed(self, app: FastAPI) -> Iterator[Backend]:
        instance = Backend(review=True)
        app.dependency_overrides[get_agent_workflows] = instance.workflows
        app.dependency_overrides[get_checkpointer] = instance.runs
        yield instance
        app.dependency_overrides.clear()

    async def start(self, client: AsyncClient, auth: dict[str, str]) -> str:
        response = await client.post(
            "/api/v1/agents/postmortem-drafting/runs",
            json={"input": "09:00 drain started\n09:12 eviction stalled"},
            headers=auth,
        )
        return response.headers["X-Paimon-Thread-Id"]

    async def test_the_run_reports_itself_as_waiting(
        self, client: AsyncClient, reviewed: Backend, auth: dict[str, str]
    ) -> None:
        await reviewed.index()
        thread_id = await self.start(client, auth)
        body = (await client.get(f"/api/v1/agents/runs/{thread_id}", headers=auth)).json()
        assert body["status"] == "awaiting_input"

    async def test_a_decision_finishes_the_run(
        self, client: AsyncClient, reviewed: Backend, auth: dict[str, str]
    ) -> None:
        await reviewed.index()
        thread_id = await self.start(client, auth)
        response = await client.post(
            f"/api/v1/agents/runs/{thread_id}/decision",
            json={"decision": "accept"},
            headers=auth,
        )
        assert response.status_code == 200
        assert response.json()["status"] == "succeeded"
        assert "review" in [step["name"] for step in response.json()["steps"]]

    async def test_deciding_on_a_run_that_is_not_waiting_is_a_conflict(
        self, client: AsyncClient, backend: Backend, auth: dict[str, str]
    ) -> None:
        # The request was well formed; the run was simply not in a state to
        # receive it, which is a conflict rather than a bad request.
        await backend.index()
        started = await client.post(
            "/api/v1/agents/incident-triage/runs",
            json={"input": "eviction hangs"},
            headers=auth,
        )
        thread_id = started.headers["X-Paimon-Thread-Id"]
        response = await client.post(
            f"/api/v1/agents/runs/{thread_id}/decision",
            json={"decision": "accept"},
            headers=auth,
        )
        assert response.status_code == 409

    async def test_another_tenant_cannot_answer_your_run(
        self, client: AsyncClient, reviewed: Backend, auth: dict[str, str]
    ) -> None:
        await reviewed.index()
        thread_id = await self.start(client, auth)
        other = DevIdentityProvider(
            signing_key="test-signing-key-padded-to-thirty-two-bytes"
        ).issue(subject="user-2", tenant_id="tenant-2")
        response = await client.post(
            f"/api/v1/agents/runs/{thread_id}/decision",
            json={"decision": "accept"},
            headers={"Authorization": f"Bearer {other}"},
        )
        assert response.status_code == 404

    async def test_an_empty_decision_is_rejected(
        self, client: AsyncClient, reviewed: Backend, auth: dict[str, str]
    ) -> None:
        await reviewed.index()
        thread_id = await self.start(client, auth)
        response = await client.post(
            f"/api/v1/agents/runs/{thread_id}/decision",
            json={"decision": ""},
            headers=auth,
        )
        assert response.status_code == 422
