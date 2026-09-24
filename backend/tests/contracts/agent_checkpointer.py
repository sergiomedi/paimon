"""Contract for the AgentCheckpointer port.

Behaviour, not quality. These assertions say what any store must do for a run to
be resumable and auditable; they say nothing about how fast it is or where it
keeps the bytes.
"""

from datetime import UTC, datetime, timedelta

import pytest

from paimon.domain.entities import AgentRun, AgentStep, RunStatus
from paimon.domain.ports import AgentCheckpointer
from paimon.domain.value_objects import Citation

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


def citation(marker: int = 1, *, document_id: str = "runbook") -> Citation:
    """Build a citation with a resolvable span, for use in a contract test."""
    return Citation(
        marker=marker,
        document_id=document_id,
        chunk_id=f"{document_id}:{marker}",
        source_uri=f"https://example.test/{document_id}",
        title="Node maintenance",
        heading_path=("Node maintenance", "Draining"),
        start_char=40,
        end_char=62,
        quote="Cordon the node first.",
    )


def run(  # noqa: PLR0913  a builder for tests: every field is one the contract pins
    thread_id: str,
    *,
    tenant_id: str = TENANT,
    status: RunStatus = RunStatus.RUNNING,
    steps: tuple[AgentStep, ...] = (),
    answer: str = "",
    citations: tuple[Citation, ...] = (),
    minutes: int = 0,
) -> AgentRun:
    """Build a run for use in a contract test."""
    return AgentRun(
        thread_id=thread_id,
        agent="triage",
        tenant_id=tenant_id,
        status=status,
        answer=answer,
        citations=citations,
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

    async def test_an_answers_citations_survive_being_stored(
        self, checkpointer: AgentCheckpointer
    ) -> None:
        # The platform's promise is that an answer carries citations or is not
        # returned. A store that kept the answer and dropped them would keep the
        # half a reader cannot check and discard the half they can.
        await checkpointer.save(
            run(
                "t-cited",
                status=RunStatus.SUCCEEDED,
                answer="Cordon the node first [1].",
                citations=(citation(),),
            )
        )

        loaded = await checkpointer.load("t-cited")

        assert loaded is not None
        assert loaded.citations == (citation(),)

    async def test_a_citations_span_survives_exactly(self, checkpointer: AgentCheckpointer) -> None:
        # Offsets are the whole difference between a citation and a filename:
        # they are what lets a reader be shown the passage in context, and what
        # lets a benchmark check the claim without asking a model.
        await checkpointer.save(run("t-span", status=RunStatus.SUCCEEDED, citations=(citation(),)))

        loaded = await checkpointer.load("t-span")

        assert loaded is not None
        stored = loaded.citations[0]
        assert (stored.start_char, stored.end_char) == (40, 62)
        assert stored.quote == "Cordon the node first."
        assert stored.heading_path == ("Node maintenance", "Draining")

    async def test_several_citations_keep_their_order(
        self, checkpointer: AgentCheckpointer
    ) -> None:
        # Marker order is the order the answer referred to them in, and an
        # answer's "[1]" has to keep meaning the first one.
        cited = (citation(1), citation(2, document_id="incident"))
        await checkpointer.save(run("t-many", status=RunStatus.SUCCEEDED, citations=cited))

        loaded = await checkpointer.load("t-many")

        assert loaded is not None
        assert [item.marker for item in loaded.citations] == [1, 2]
        assert [item.document_id for item in loaded.citations] == ["runbook", "incident"]

    async def test_a_run_that_cited_nothing_loads_as_having_cited_nothing(
        self, checkpointer: AgentCheckpointer
    ) -> None:
        # Empty is a meaningful value, not a missing one: a run that refused
        # cites nothing, and that is the correct record of what it did.
        await checkpointer.save(run("t-refused", status=RunStatus.SUCCEEDED, answer="No material."))

        loaded = await checkpointer.load("t-refused")

        assert loaded is not None
        assert loaded.citations == ()
        assert not loaded.grounded

    async def test_replacing_a_run_replaces_its_citations(
        self, checkpointer: AgentCheckpointer
    ) -> None:
        # A run is upserted after every step. A node that withdraws a draft
        # writes empty citations with it, so the record must not end up holding
        # a refusal beside the citations of the answer it replaced.
        await checkpointer.save(
            run("t-withdrawn", answer="Cordon the node first [1].", citations=(citation(),))
        )
        await checkpointer.save(
            run("t-withdrawn", status=RunStatus.SUCCEEDED, answer="I could not support that.")
        )

        loaded = await checkpointer.load("t-withdrawn")

        assert loaded is not None
        assert loaded.citations == ()
