"""Documentation gap analysis."""

from tests.unit.agents.conftest import TENANT, Harness

from paimon.agents.gaps import (
    AGENT_NAME,
    ASPECTS,
    NOTHING_INDEXED,
    SURVEY_FAILED,
    build_gaps_graph,
)
from paimon.domain.agents import AgentState, NodeSpec
from paimon.domain.entities import RunStatus

COVERAGE = (
    "The sources describe the symptoms and the mitigation [1]. "
    "Detection, rollback, ownership and escalation are not documented."
)


def graph(harness: Harness) -> object:
    return build_gaps_graph(
        harness.retrieve, harness.chat_model, harness.repository, harness.token_counter
    )


def node(harness: Harness, name: str) -> NodeSpec:
    spec = build_gaps_graph(
        harness.retrieve, harness.chat_model, harness.repository, harness.token_counter
    )
    return next(item for item in spec.nodes if item.name == name)


async def run(harness: Harness, topic: str = "node draining") -> list[str]:
    return [
        step.name
        async for step in harness.workflow(graph(harness)).stream(
            topic, thread_id="t-1", tenant_id=TENANT
        )
    ]


class TestTheChecklist:
    def test_the_aspects_are_fixed_in_code(self) -> None:
        # The point of a fixed checklist: two runs on two topics, or one topic
        # two months apart, answer the same question. A model-invented list
        # makes every report incomparable with every other.
        assert ASPECTS == (
            "symptoms",
            "detection",
            "mitigation",
            "rollback",
            "ownership",
            "escalation",
        )

    async def test_every_aspect_is_named_in_the_prompt(self) -> None:
        harness = Harness()
        await harness.index()
        await run(harness)
        asked = harness.chat_model.calls[-1][-1].content
        for aspect in ASPECTS:
            assert aspect in asked


class TestAnEmptyCorpus:
    async def test_nothing_indexed_is_a_finding_not_a_refusal(self) -> None:
        harness = Harness()
        names = await run(harness)
        assert names == ["survey", "report_nothing"]

    async def test_it_reports_without_calling_a_model(self) -> None:
        harness = Harness()
        await run(harness)
        assert harness.chat_model.calls == []

    async def test_the_finding_says_everything_is_undocumented(self) -> None:
        harness = Harness()
        update = await node(harness, "report_nothing").run(
            AgentState(question="node draining", tenant_id=TENANT)
        )
        assert update["draft"] == NOTHING_INDEXED


class TestWhenTheSurveyBreaks:
    """The asymmetry that makes this agent useful also makes it dangerous."""

    async def test_a_broken_search_is_not_reported_as_undocumented(self) -> None:
        # Finding nothing IS the finding here, so a search that failed silently
        # becomes the strongest possible claim about the documentation. It is
        # the one failure mode where being wrong looks like being right.
        harness = Harness(reachable=False)
        names = await run(harness)
        assert names[-1] == "report_failure"

    async def test_it_says_so_plainly(self) -> None:
        harness = Harness(reachable=False)
        update = await node(harness, "report_failure").run(
            AgentState(question="node draining", tenant_id=TENANT)
        )
        assert update["draft"] == SURVEY_FAILED
        assert update["draft"] != NOTHING_INDEXED

    async def test_no_model_is_called(self) -> None:
        harness = Harness(reachable=False)
        await run(harness)
        assert harness.chat_model.calls == []


class TestCoverage:
    async def test_it_records_which_aspects_the_report_addressed(self) -> None:
        harness = Harness(answer=COVERAGE)
        await harness.index()
        steps = [
            step
            async for step in harness.workflow(graph(harness)).stream(
                "node draining", thread_id="t-1", tenant_id=TENANT
            )
        ]
        summarised = next(step for step in steps if step.name == "summarise")
        assert summarised.summary == f"{len(ASPECTS)} of {len(ASPECTS)} aspects addressed"

    async def test_a_report_that_cites_nothing_is_withdrawn(self) -> None:
        # A gap report with no citations is a report about the model, not about
        # the corpus, and the two are indistinguishable to a reader.
        harness = Harness()
        state = AgentState(question="node draining", tenant_id=TENANT, draft=COVERAGE, citations=())
        assert (await node(harness, "summarise").run(state))["draft"] == NOTHING_INDEXED

    async def test_a_cited_report_survives(self) -> None:
        harness = Harness(answer=COVERAGE)
        await harness.index()
        await run(harness)
        stored = await harness.checkpointer.load("t-1")
        assert stored is not None
        assert stored.agent == AGENT_NAME
        assert stored.status is RunStatus.SUCCEEDED
