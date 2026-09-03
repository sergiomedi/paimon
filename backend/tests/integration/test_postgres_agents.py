"""The agent persistence adapters, run against the same contracts as the fakes."""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from paimon.domain.entities import AgentRun, AgentStep, RunStatus
from paimon.domain.ports import AgentCheckpointer, AgentMemory
from paimon.infrastructure.persistence import PgVectorAgentMemory, PostgresCheckpointer
from paimon.infrastructure.persistence.models.rag import EMBEDDING_DIMENSIONS
from tests.contracts.agent_checkpointer import AgentCheckpointerContract, run, step
from tests.contracts.agent_memory import AgentMemoryContract
from tests.fakes import FakeEmbeddingModel

pytestmark = pytest.mark.integration

TENANT = "tenant-a"


@pytest.fixture
async def clean_agent_tables(engine: AsyncEngine, migrated_database: None) -> None:
    """Start every test from an empty store."""
    async with engine.begin() as connection:
        await connection.execute(text("TRUNCATE TABLE agent_runs, agent_memories"))


class TestPostgresCheckpointer(AgentCheckpointerContract):
    @pytest.fixture
    def checkpointer(self, engine: AsyncEngine, clean_agent_tables: None) -> AgentCheckpointer:
        return PostgresCheckpointer(engine)


class TestPgVectorAgentMemory(AgentMemoryContract):
    @pytest.fixture
    def memory(self, engine: AsyncEngine, clean_agent_tables: None) -> AgentMemory:
        # The column is vector(1024), so the fake produces the production width
        # and the HNSW index is exercised at the size deployments use.
        return PgVectorAgentMemory(
            engine, FakeEmbeddingModel(dimensions=EMBEDDING_DIMENSIONS), TENANT
        )


class TestRoundTrippingThroughJson:
    """What the in-memory implementation cannot check, because it never encodes."""

    async def test_a_timestamp_comes_back_timezone_aware(
        self, engine: AsyncEngine, clean_agent_tables: None
    ) -> None:
        # AgentStep refuses naive timestamps, so a driver that drops the offset
        # would turn a successful save into a failure on the next load.
        checkpointer = PostgresCheckpointer(engine)
        await checkpointer.save(run("t-1", steps=(step("frame"),)))
        loaded = await checkpointer.load("t-1")
        assert loaded is not None
        assert loaded.started_at.tzinfo is not None
        assert loaded.steps[0].started_at.tzinfo is not None

    async def test_step_details_survive_as_strings(
        self, engine: AsyncEngine, clean_agent_tables: None
    ) -> None:
        checkpointer = PostgresCheckpointer(engine)
        detailed = AgentStep(
            name="assess",
            summary="weighed the evidence",
            started_at=step("assess").started_at,
            finished_at=step("assess").finished_at,
            details={"chunks": "2", "documents": "2"},
        )
        await checkpointer.save(
            AgentRun(
                thread_id="t-1",
                agent="triage",
                tenant_id=TENANT,
                status=RunStatus.SUCCEEDED,
                steps=(detailed,),
            )
        )
        loaded = await checkpointer.load("t-1")
        assert loaded is not None
        assert loaded.steps[0].details == {"chunks": "2", "documents": "2"}

    async def test_a_nested_namespace_is_not_matched_by_its_prefix(
        self, engine: AsyncEngine, clean_agent_tables: None
    ) -> None:
        # The reason the namespace is a text[] rather than a delimited string:
        # array equality has no delimiter to be confused by.
        memory = PgVectorAgentMemory(
            engine, FakeEmbeddingModel(dimensions=EMBEDDING_DIMENSIONS), TENANT
        )
        await memory.remember(("incidents",), "a", {"summary": "a drain stalled"})
        await memory.remember(("incidents", "network"), "b", {"summary": "a drain stalled"})
        assert len(await memory.recall(("incidents",), "drain")) == 1

    async def test_memories_are_isolated_by_tenant(
        self, engine: AsyncEngine, clean_agent_tables: None
    ) -> None:
        model = FakeEmbeddingModel(dimensions=EMBEDDING_DIMENSIONS)
        mine = PgVectorAgentMemory(engine, model, TENANT)
        theirs = PgVectorAgentMemory(engine, model, "tenant-b")
        await mine.remember(("incidents",), "a", {"summary": "a drain stalled"})
        assert list(await theirs.recall(("incidents",), "drain")) == []
