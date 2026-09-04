"""Contract for the AgentCheckpointer port.

Behaviour, not quality. These assertions say what any store must do for a run to
be resumable and auditable; they say nothing about how fast it is or where it
keeps the bytes.
"""

from datetime import UTC, datetime, timedelta

import pytest

from paimon.domain.entities import AgentRun, AgentStep, RunStatus
from paimon.domain.ports import AgentCheckpointer

TENANT = "tenant-a"
OTHER_TENANT = "tenant-b"


def step(name: str, *, minutes: int = 0, tokens: int = 0) -> AgentStep:
    """Build a step at a fixed point in time, for reproducible assertions."""
    began = datetime(2026, 9, 3, 9, 0, tzinfo=UTC) + timedelta(minutes=minutes)
    return AgentStep(
        name=name,
        summary=f"{name} ran",
        started_at=began,
        finished_at=began + timedelta(seconds=1),
        input_tokens=tokens,
    )


def run(  # noqa: PLR0913  a builder for tests: every field is one the contract pins
    thread_id: str,
    *,
    tenant_id: str = TENANT,
    status: RunStatus = RunStatus.RUNNING,
    steps: tuple[AgentStep, ...] = (),
    answer: str = "",
    minutes: int = 0,
) -> AgentRun:
    """Build a run for use in a contract test."""
    return AgentRun(
        thread_id=thread_id,
        agent="triage",
        tenant_id=tenant_id,
        status=status,
        answer=answer,
        steps=steps,
        started_at=datetime(2026, 9, 3, 9, 0, tzinfo=UTC) + timedelta(minutes=minutes),
    )


class AgentCheckpointerContract:
    """Every AgentCheckpointer adapter must pass these."""

    @pytest.fixture
    def checkpointer(self) -> AgentCheckpointer:
        """Supplied by the subclass, empty and ready to write to."""
        raise NotImplementedError

    async def test_a_saved_run_loads_back(self, checkpointer: AgentCheckpointer) -> None:
        await checkpointer.save(run("t-1"))
        loaded = await checkpointer.load("t-1")
        assert loaded is not None
        assert loaded.thread_id == "t-1"
        assert loaded.agent == "triage"
        assert loaded.status is RunStatus.RUNNING

    async def test_an_unknown_thread_loads_as_none(self, checkpointer: AgentCheckpointer) -> None:
        assert await checkpointer.load("never-started") is None

    async def test_saving_again_replaces_the_earlier_state(
        self, checkpointer: AgentCheckpointer
    ) -> None:
        await checkpointer.save(run("t-1", steps=(step("retrieve"),)))
        await checkpointer.save(
            run("t-1", status=RunStatus.SUCCEEDED, steps=(step("retrieve"), step("answer")))
        )
        loaded = await checkpointer.load("t-1")
        assert loaded is not None
        assert loaded.status is RunStatus.SUCCEEDED
        assert [item.name for item in loaded.steps] == ["retrieve", "answer"]

    async def test_steps_survive_the_round_trip_in_order(
        self, checkpointer: AgentCheckpointer
    ) -> None:
        await checkpointer.save(
            run("t-1", steps=(step("retrieve", minutes=0), step("answer", minutes=1)))
        )
        loaded = await checkpointer.load("t-1")
        assert loaded is not None
        assert [item.name for item in loaded.steps] == ["retrieve", "answer"]
        assert loaded.steps[0].started_at < loaded.steps[1].started_at

    async def test_token_counts_survive_the_round_trip(
        self, checkpointer: AgentCheckpointer
    ) -> None:
        await checkpointer.save(run("t-1", steps=(step("answer", tokens=120),)))
        loaded = await checkpointer.load("t-1")
        assert loaded is not None
        assert loaded.total_tokens == 120

    async def test_what_the_run_produced_survives_the_round_trip(
        self, checkpointer: AgentCheckpointer
    ) -> None:
        # A run that reads back without its answer is a run whose result existed
        # once, for whoever was watching. That is a notification, not a record.
        await checkpointer.save(run("t-1", answer="Cordon the node first [1]."))
        loaded = await checkpointer.load("t-1")
        assert loaded is not None
        assert loaded.answer == "Cordon the node first [1]."

    async def test_a_later_save_replaces_the_answer(self, checkpointer: AgentCheckpointer) -> None:
        # A node that withdraws a draft writes over it, so the record shows the
        # withdrawal rather than both.
        await checkpointer.save(run("t-1", answer="a draft"))
        await checkpointer.save(run("t-1", answer="withdrawn: it cites nothing"))
        loaded = await checkpointer.load("t-1")
        assert loaded is not None
        assert loaded.answer == "withdrawn: it cites nothing"

    async def test_a_suspended_run_keeps_its_status(self, checkpointer: AgentCheckpointer) -> None:
        await checkpointer.save(run("t-1", status=RunStatus.AWAITING_INPUT))
        loaded = await checkpointer.load("t-1")
        assert loaded is not None
        assert loaded.status is RunStatus.AWAITING_INPUT
        assert not loaded.is_terminal

    async def test_listing_is_scoped_to_one_tenant(self, checkpointer: AgentCheckpointer) -> None:
        await checkpointer.save(run("t-1"))
        await checkpointer.save(run("t-2", tenant_id=OTHER_TENANT))
        listed = await checkpointer.list_runs(TENANT)
        assert [item.thread_id for item in listed] == ["t-1"]

    async def test_listing_returns_most_recent_first(self, checkpointer: AgentCheckpointer) -> None:
        await checkpointer.save(run("older", minutes=0))
        await checkpointer.save(run("newer", minutes=5))
        listed = await checkpointer.list_runs(TENANT)
        assert [item.thread_id for item in listed] == ["newer", "older"]

    async def test_listing_respects_the_limit(self, checkpointer: AgentCheckpointer) -> None:
        for index in range(5):
            await checkpointer.save(run(f"t-{index}", minutes=index))
        assert len(await checkpointer.list_runs(TENANT, limit=2)) == 2

    async def test_listing_an_unknown_tenant_is_empty_not_an_error(
        self, checkpointer: AgentCheckpointer
    ) -> None:
        assert list(await checkpointer.list_runs("tenant-nobody")) == []
