"""Suspending a run for a person, and continuing it afterwards.

Run against LangGraph's in-memory saver rather than a mock. Suspension is the
one behaviour where the framework does the work and the adapter only asks for
it, so a double would be testing the request rather than the result.
"""

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from paimon.domain.agents import END, AgentState, GraphSpec, NodeSpec, StateUpdate
from paimon.domain.entities import RunStatus
from paimon.domain.errors import UnknownThreadError
from paimon.domain.ports import HumanInTheLoop
from paimon.infrastructure.orchestration import LangGraphWorkflow
from tests.fakes import InMemoryCheckpointer

TENANT = "tenant-a"


async def propose(state: AgentState) -> StateUpdate:
    return {"draft": f"proposed action for {state.question}"}


async def ask(state: AgentState) -> StateUpdate:
    """Suspend by writing to the state, not by calling the runtime."""
    return {"awaiting": f"Approve this action? {state.draft}"}


async def apply(state: AgentState) -> StateUpdate:
    return {"draft": f"{state.draft} [decision: {state.decision}]"}


def spec() -> GraphSpec:
    return GraphSpec(
        name="approver",
        entry="propose",
        nodes=[
            NodeSpec(name="propose", run=propose, summary="proposed an action"),
            NodeSpec(name="ask", run=ask, summary="asked for approval"),
            NodeSpec(name="apply", run=apply, summary="applied the decision"),
        ],
        edges=[("propose", "ask"), ("ask", "apply"), ("apply", END)],
    )


def build(*, resumable: bool = True) -> tuple[LangGraphWorkflow, InMemoryCheckpointer]:
    runs = InMemoryCheckpointer()
    workflow = LangGraphWorkflow(spec(), runs, saver=InMemorySaver() if resumable else None)
    return workflow, runs


async def start(workflow: LangGraphWorkflow, thread_id: str = "t-1") -> list[str]:
    return [
        step.name
        async for step in workflow.stream(
            "restart the worker", thread_id=thread_id, tenant_id=TENANT
        )
    ]


class TestSuspending:
    async def test_the_run_stops_before_the_asking_node_is_recorded(self) -> None:
        # Only "propose" is emitted, and this is the framework being precise
        # rather than losing something: interrupting discards the asking node's
        # work, and the node runs again from the top when the run is resumed. Its
        # step therefore belongs to the resumed half, not to this one. A node
        # that suspends should not be a node that called a model.
        workflow, _ = build()
        assert await start(workflow) == ["propose"]

    async def test_it_is_recorded_as_awaiting_input(self) -> None:
        # Not succeeded and not failed. An operator who cannot see this state
        # will never answer, and the run waits forever.
        workflow, runs = build()
        await start(workflow)
        run = await runs.load("t-1")
        assert run is not None
        assert run.status is RunStatus.AWAITING_INPUT
        assert not run.is_terminal

    async def test_nothing_past_the_question_has_run(self) -> None:
        workflow, runs = build()
        await start(workflow)
        run = await runs.load("t-1")
        assert run is not None
        assert [step.name for step in run.steps] == ["propose"]


class TestResuming:
    async def test_the_decision_reaches_the_node_that_needed_it(self) -> None:
        workflow, _ = build()
        await start(workflow)
        resumed = await workflow.resume("approved", thread_id="t-1")
        assert resumed.status is RunStatus.SUCCEEDED
        assert [step.name for step in resumed.steps][-1] == "apply"

    async def test_the_trace_spans_both_halves_of_the_run(self) -> None:
        # The steps taken before the pause are still there afterwards: one run,
        # interrupted, not two runs stitched together.
        workflow, _ = build()
        await start(workflow)
        resumed = await workflow.resume("approved", thread_id="t-1")
        assert [step.name for step in resumed.steps] == ["propose", "ask", "apply"]

    async def test_resuming_a_thread_that_never_existed_is_refused(self) -> None:
        workflow, _ = build()
        with pytest.raises(UnknownThreadError, match="no run 'never-started'"):
            await workflow.resume("approved", thread_id="never-started")

    async def test_a_deployment_without_graph_state_says_so(self) -> None:
        # Rather than failing somewhere inside the framework with a message
        # about a missing checkpointer.
        workflow, _ = build(resumable=False)
        await start(workflow)
        with pytest.raises(UnknownThreadError, match="without graph checkpointing"):
            await workflow.resume("approved", thread_id="t-1")


class TestTheCapability:
    def test_the_workflow_declares_it(self) -> None:
        workflow, _ = build()
        assert isinstance(workflow, HumanInTheLoop)

    async def test_a_graph_that_never_asks_runs_straight_through(self) -> None:
        # The capability costs nothing when it is not used: a workflow with no
        # suspending node behaves exactly as it did before.
        runs = InMemoryCheckpointer()
        plain = GraphSpec(
            name="plain",
            entry="propose",
            nodes=[NodeSpec(name="propose", run=propose)],
            edges=[("propose", END)],
        )
        workflow = LangGraphWorkflow(plain, runs, saver=InMemorySaver())
        assert await start(workflow) == ["propose"]
        run = await runs.load("t-1")
        assert run is not None
        assert run.status is RunStatus.SUCCEEDED
