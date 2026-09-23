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


async def refuse_differently(state: AgentState) -> StateUpdate:
    msg = "the index is missing"
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

    async def test_two_branches_can_fail_at_once(self) -> None:
        # Found by running the platform against an unreachable provider, not by
        # a test: two parallel retrievals both failed, both wrote the failure,
        # and the orchestrator refused two writes to one field in one step. A
        # graph that fans out has no exotic case here - it is the ordinary one,
        # because whatever made the first branch fail is usually still true for
        # the second.
        checkpointer = InMemoryCheckpointer()
        spec = GraphSpec(
            name="fanout",
            entry="frame",
            nodes=[
                NodeSpec(name="frame", run=retrieve),
                NodeSpec(name="left", run=explode),
                NodeSpec(name="right", run=explode),
                NodeSpec(name="join", run=retrieve),
            ],
            edges=[
                ("frame", "left"),
                ("frame", "right"),
                ("left", "join"),
                ("right", "join"),
                ("join", END),
            ],
        )
        names = await steps_of(LangGraphWorkflow(spec, checkpointer))

        assert sorted(names) == ["frame", "join", "left", "right"]
        run = await checkpointer.load("t-1")
        assert run is not None
        assert run.status is RunStatus.FAILED

    async def test_both_reasons_are_kept_when_two_branches_fail(self) -> None:
        # When two branches fail differently, the second is usually what
        # explains the first.
        spec = GraphSpec(
            name="fanout",
            entry="frame",
            nodes=[
                NodeSpec(name="frame", run=retrieve),
                NodeSpec(name="left", run=explode),
                NodeSpec(name="right", run=refuse_differently),
                NodeSpec(name="join", run=retrieve),
            ],
            edges=[
                ("frame", "left"),
                ("frame", "right"),
                ("left", "join"),
                ("right", "join"),
                ("join", END),
            ],
        )
        checkpointer = InMemoryCheckpointer()
        await steps_of(LangGraphWorkflow(spec, checkpointer))
        run = await checkpointer.load("t-1")
        assert run is not None
        summaries = " ".join(step.summary for step in run.steps)
        assert "the provider refused" in summaries
        assert "the index is missing" in summaries

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

    async def test_the_step_limit_leaves_a_failed_run_with_the_steps_it_managed(self) -> None:
        # What the limit costs, stated rather than assumed. An agent that loops
        # until the framework stops it does not produce a result: it produces a
        # FAILED run. That is the whole reason an agent with a loop has to own a
        # budget of its own and stop before this fires — this is the backstop,
        # not the mechanism.
        checkpointer = InMemoryCheckpointer()
        spec = GraphSpec(
            name="looper",
            entry="retrieve",
            nodes=[NodeSpec(name="retrieve", run=retrieve)],
            edges=[("retrieve", "retrieve")],
        )
        workflow = LangGraphWorkflow(spec, checkpointer, step_limit=3)
        with pytest.raises(AgentRunError):
            await steps_of(workflow)

        run = await checkpointer.load("t-1")
        assert run is not None
        assert run.status is RunStatus.FAILED
        assert len(run.steps) == 3


class TestCycles:
    """A branch that leads back to an earlier node, run for real.

    The shape every tool-calling agent has, and until Phase 9 nothing here ran
    one. The existing step-limit test proves a runaway loop is stopped; these
    prove a *terminating* loop does what its author meant, which is the
    different and more useful claim.
    """

    @staticmethod
    def _counting_loop(rounds: int) -> GraphSpec:
        """A graph that goes act -> tools -> act until it has looped enough."""

        async def act(state: AgentState) -> StateUpdate:
            return {"notes": state.notes + "x", "usage": (3, 1)}

        async def tools(_state: AgentState) -> StateUpdate:
            return {}

        async def finalize(state: AgentState) -> StateUpdate:
            return {"draft": f"answered after {len(state.notes)} turns"}

        return GraphSpec(
            name="looper",
            entry="act",
            nodes=[
                NodeSpec(name="act", run=act, summary="called the model"),
                NodeSpec(name="tools", run=tools, summary="ran the tools"),
                NodeSpec(name="finalize", run=finalize, summary="answered"),
            ],
            edges=[("tools", "act"), ("finalize", END)],
            branches=[
                Branch(
                    source="act",
                    decide=lambda state: "tools" if len(state.notes) < rounds else "finalize",
                    targets={"tools": "tools", "finalize": "finalize"},
                )
            ],
        )

    async def test_a_node_reached_twice_runs_twice(self) -> None:
        workflow = LangGraphWorkflow(self._counting_loop(3), InMemoryCheckpointer())
        assert await steps_of(workflow) == ["act", "tools", "act", "tools", "act", "finalize"]

    async def test_every_pass_leaves_its_own_step(self) -> None:
        # The append reducer, doing the thing it exists for. Replacement would
        # leave a three-turn run remembering one turn, which is the trace an
        # operator would be asked to investigate an overspend with.
        checkpointer = InMemoryCheckpointer()
        workflow = LangGraphWorkflow(self._counting_loop(3), checkpointer)
        async for _ in workflow.stream("why?", thread_id="t-1", tenant_id="tenant-a"):
            pass

        run = await checkpointer.load("t-1")
        assert run is not None
        assert run.status is RunStatus.SUCCEEDED
        assert [step.name for step in run.steps].count("act") == 3

    async def test_usage_accumulates_across_passes(self) -> None:
        # What makes a token budget expressible as a branch: state.usage is the
        # running total, not the last node's share.
        checkpointer = InMemoryCheckpointer()
        workflow = LangGraphWorkflow(self._counting_loop(3), checkpointer)
        async for _ in workflow.stream("why?", thread_id="t-1", tenant_id="tenant-a"):
            pass

        run = await checkpointer.load("t-1")
        assert run is not None
        assert run.total_tokens == 12

    async def test_a_loop_that_stops_reaches_its_answer(self) -> None:
        checkpointer = InMemoryCheckpointer()
        workflow = LangGraphWorkflow(self._counting_loop(2), checkpointer)
        async for _ in workflow.stream("why?", thread_id="t-1", tenant_id="tenant-a"):
            pass

        run = await checkpointer.load("t-1")
        assert run is not None
        assert run.answer == "answered after 2 turns"


async def test_the_workflow_satisfies_the_port() -> None:
    workflow = LangGraphWorkflow(two_step(), InMemoryCheckpointer())
    assert isinstance(workflow, AgentWorkflow)
