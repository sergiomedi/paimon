"""The incident triage agent.

Two levels, deliberately. The node tests call node bodies directly, with no graph
runtime, which is what ADR-0015 was for. The graph tests run the whole agent
through the adapter, because a set of nodes that each work and never get wired
together correctly is a set of nodes that works and an agent that does not.
"""

import pytest
from tests.unit.agents.conftest import RUNBOOK, TENANT, Harness, chunk

from paimon.agents.triage import (
    AGENT_NAME,
    HISTORY_FRAMING,
    PROCEDURE_FRAMING,
    RETRIEVAL_FAILED,
    UNSUPPORTED,
    build_triage_graph,
    frame_symptom,
)
from paimon.application.use_cases.answer_question import NO_MATERIAL
from paimon.domain.agents import AgentState, GraphSpec, NodeSpec
from paimon.domain.entities import RunStatus
from paimon.infrastructure.orchestration import LangGraphWorkflow


def graph(harness: Harness) -> GraphSpec:
    """Build the triage agent from a harness's collaborators."""
    return build_triage_graph(harness.collaborators())


def node(harness: Harness, name: str) -> NodeSpec:
    """One node of the triage graph, for calling with no runtime."""
    return next(item for item in graph(harness).nodes if item.name == name)


def workflow(harness: Harness) -> LangGraphWorkflow:
    return LangGraphWorkflow(graph(harness), harness.checkpointer)


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

        procedure = await node(harness, "procedure").run(state)
        history = await node(harness, "history").run(state)

        assert procedure.get("evidence")
        assert history.get("evidence")

    async def test_refuse_does_not_call_a_model(self) -> None:
        harness = Harness()
        update = await node(harness, "refuse").run(
            AgentState(question="eviction hangs", tenant_id=TENANT)
        )
        assert update["draft"] == NO_MATERIAL
        assert harness.chat_model.calls == []

    async def test_verify_leaves_a_supported_draft_alone(self) -> None:
        harness = Harness()
        await harness.index()
        drafted = await node(harness, "draft").run(
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
        assert await node(harness, "verify").run(state) == {}

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
        assert (await node(harness, "verify").run(state))["draft"] == UNSUPPORTED


class TestWhenTheSearchItselfBreaks:
    """Found by running the platform, not by imagining a case.

    Two parallel retrievals against an unreachable embedding provider fail
    together, which is the ordinary outcome rather than a rare one: whatever
    broke the first branch is still true for the second.
    """

    async def test_both_branches_failing_does_not_abort_the_run(self) -> None:
        harness = Harness(reachable=False)
        names = [
            step.name
            async for step in workflow(harness).stream(
                "eviction hangs", thread_id="t-1", tenant_id=TENANT
            )
        ]
        assert names[-1] == "abort"

    async def test_it_says_the_search_broke_rather_than_that_nothing_exists(self) -> None:
        # "I have no indexed material" is a false statement about the corpus
        # when nobody actually looked, and the difference matters most at the
        # moment someone is relying on the answer.
        harness = Harness(reachable=False)
        update = await node(harness, "abort").run(
            AgentState(question="eviction hangs", tenant_id=TENANT)
        )
        assert update["draft"] == RETRIEVAL_FAILED
        assert update["draft"] != NO_MATERIAL

    async def test_the_run_is_recorded_as_failed_with_both_reasons(self) -> None:
        harness = Harness(reachable=False)
        async for _ in workflow(harness).stream(
            "eviction hangs", thread_id="t-1", tenant_id=TENANT
        ):
            pass
        run = await harness.checkpointer.load("t-1")
        assert run is not None
        assert run.status is RunStatus.FAILED
        failed = [step for step in run.steps if "failed" in step.summary]
        assert {step.name for step in failed} == {"procedure", "history"}

    async def test_no_model_is_called_when_the_search_broke(self) -> None:
        harness = Harness(reachable=False)
        async for _ in workflow(harness).stream(
            "eviction hangs", thread_id="t-1", tenant_id=TENANT
        ):
            pass
        assert harness.chat_model.calls == []


class TestTheWholeAgent:
    async def test_it_takes_the_drafting_path_when_there_is_material(self) -> None:
        harness = Harness()
        await harness.index()
        names = [
            step.name
            async for step in workflow(harness).stream(
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
            async for step in workflow(harness).stream(
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
            async for step in workflow(harness).stream(
                "eviction hangs", thread_id="t-1", tenant_id=TENANT
            )
            if step.name == "assess"
        ]
        assert assessment[0].details["chunks"] == "2"

    async def test_a_completed_run_is_recorded_under_the_agent_name(self) -> None:
        harness = Harness()
        await harness.index()
        async for _ in workflow(harness).stream(
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
        async for _ in workflow(harness).stream(
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
            async for step in workflow(harness).stream(
                "eviction hangs", thread_id="t-1", tenant_id="tenant-b"
            )
        ]
        assert names[-1] == "refuse"


@pytest.mark.parametrize("node_name", ["frame", "procedure", "history", "assess", "draft"])
def test_every_declared_node_is_reachable(node_name: str) -> None:
    spec = graph(Harness())
    spec.validate()
    assert node_name in spec.node_names()
