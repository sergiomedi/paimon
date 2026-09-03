"""The orchestration adapter, run against real graphs.

These use LangGraph for real rather than mocking it. Mocking the framework here
would test that the adapter calls the methods the author believed exist, which is
the assumption most worth checking.
"""

import pytest

from paimon.domain.agents import (
    END,
    AgentState,
    Branch,
    GraphSpec,
    NodeSpec,
    StateUpdate,
    StepReport,
)
from paimon.domain.entities import RunStatus
from paimon.domain.errors import AgentRunError
from paimon.domain.ports import AgentWorkflow
from paimon.infrastructure.orchestration import LangGraphWorkflow
from tests.fakes import InMemoryCheckpointer


async def retrieve(state: AgentState) -> StateUpdate:
    return {"draft": f"material for {state.question}"}


async def answer(state: AgentState) -> StateUpdate:
    return {"draft": state.draft.upper()}


def count_tokens(_state: AgentState, _update: StateUpdate) -> StepReport:
    return StepReport(input_tokens=80, output_tokens=20)


async def explode(state: AgentState) -> StateUpdate:
    msg = "the provider refused"
    raise RuntimeError(msg)


def two_step() -> GraphSpec:
    return GraphSpec(
        name="triage",
        entry="retrieve",
        nodes=[
            NodeSpec(name="retrieve", run=retrieve, summary="looked for material"),
            NodeSpec(name="answer", run=answer, summary="drafted an answer"),
        ],
        edges=[("retrieve", "answer"), ("answer", END)],
    )


async def steps_of(workflow: LangGraphWorkflow, question: str = "why?") -> list[str]:
    return [
        step.name async for step in workflow.stream(question, thread_id="t-1", tenant_id="tenant-a")
    ]


class TestRunning:
    async def test_it_runs_the_nodes_in_order(self) -> None:
        workflow = LangGraphWorkflow(two_step(), InMemoryCheckpointer())
        assert await steps_of(workflow) == ["retrieve", "answer"]

    async def test_each_step_carries_the_summary_the_node_declared(self) -> None:
        workflow = LangGraphWorkflow(two_step(), InMemoryCheckpointer())
        summaries = [
            step.summary
            async for step in workflow.stream("why?", thread_id="t-1", tenant_id="tenant-a")
        ]
        assert summaries == ["looked for material", "drafted an answer"]

    async def test_a_step_is_timed_by_the_adapter_not_by_the_node(self) -> None:
        workflow = LangGraphWorkflow(two_step(), InMemoryCheckpointer())
        async for step in workflow.stream("why?", thread_id="t-1", tenant_id="tenant-a"):
            assert step.finished_at >= step.started_at
            assert step.started_at.tzinfo is not None

    async def test_a_finished_run_is_recorded_as_succeeded(self) -> None:
        checkpointer = InMemoryCheckpointer()
        workflow = LangGraphWorkflow(two_step(), checkpointer)
        await steps_of(workflow)
        run = await checkpointer.load("t-1")
        assert run is not None
        assert run.status is RunStatus.SUCCEEDED
        assert [item.name for item in run.steps] == ["retrieve", "answer"]

    async def test_the_run_is_checkpointed_before_it_finishes(self) -> None:
        # The point of saving per step: a process that dies mid-run leaves a
        # record of how far it got.
        checkpointer = InMemoryCheckpointer()
        workflow = LangGraphWorkflow(two_step(), checkpointer)
        seen: list[int] = []
        async for _ in workflow.stream("why?", thread_id="t-1", tenant_id="tenant-a"):
            run = await checkpointer.load("t-1")
            assert run is not None
            seen.append(len(run.steps))
        assert seen == [1, 2]

    async def test_token_counts_reported_by_a_node_reach_the_run(self) -> None:
        checkpointer = InMemoryCheckpointer()
        spec = GraphSpec(
            name="triage",
            entry="answer",
            nodes=[
                NodeSpec(
                    name="answer",
                    run=answer,
                    summary="drafted",
                    report=count_tokens,
                )
            ],
            edges=[("answer", END)],
        )
        await steps_of(LangGraphWorkflow(spec, checkpointer))
        run = await checkpointer.load("t-1")
        assert run is not None
        assert run.total_tokens == 100


class TestBranching:
    async def test_a_branch_chooses_the_path(self) -> None:
        spec = GraphSpec(
            name="triage",
            entry="retrieve",
            nodes=[
                NodeSpec(name="retrieve", run=retrieve),
                NodeSpec(name="answer", run=answer),
                NodeSpec(name="refuse", run=retrieve),
            ],
            edges=[("answer", END), ("refuse", END)],
            branches=[
                Branch(
                    source="retrieve",
                    decide=lambda state: "refuse" if "nothing" in state.question else "answer",
                    targets={"answer": "answer", "refuse": "refuse"},
                )
            ],
        )
        workflow = LangGraphWorkflow(spec, InMemoryCheckpointer())
        assert await steps_of(workflow, "nothing here") == ["retrieve", "refuse"]
        assert await steps_of(workflow, "why?") == ["retrieve", "answer"]


class TestFailure:
    async def test_a_failing_node_ends_the_run_without_losing_the_trace(self) -> None:
        checkpointer = InMemoryCheckpointer()
        spec = GraphSpec(
            name="triage",
            entry="retrieve",
            nodes=[
                NodeSpec(name="retrieve", run=retrieve),
                NodeSpec(name="answer", run=explode),
            ],
            edges=[("retrieve", "answer"), ("answer", END)],
        )
        assert await steps_of(LangGraphWorkflow(spec, checkpointer)) == ["retrieve", "answer"]
        run = await checkpointer.load("t-1")
        assert run is not None
        assert run.status is RunStatus.FAILED
        assert "the provider refused" in run.steps[-1].summary
        # The successful step before the failure is still there.
        assert run.steps[0].name == "retrieve"

    async def test_a_malformed_graph_fails_at_construction_not_on_the_first_question(
        self,
    ) -> None:
        spec = GraphSpec(name="triage", entry="nowhere", nodes=[NodeSpec("answer", answer)])
        with pytest.raises(ValueError, match="starts at 'nowhere'"):
            LangGraphWorkflow(spec, InMemoryCheckpointer())

    async def test_a_cycle_is_stopped_by_the_step_limit(self) -> None:
        spec = GraphSpec(
            name="looper",
            entry="retrieve",
            nodes=[NodeSpec(name="retrieve", run=retrieve)],
            edges=[("retrieve", "retrieve")],
        )
        workflow = LangGraphWorkflow(spec, InMemoryCheckpointer(), step_limit=3)
        with pytest.raises(AgentRunError, match="could not complete run"):
            await steps_of(workflow)


async def test_the_workflow_satisfies_the_port() -> None:
    workflow = LangGraphWorkflow(two_step(), InMemoryCheckpointer())
    assert isinstance(workflow, AgentWorkflow)
