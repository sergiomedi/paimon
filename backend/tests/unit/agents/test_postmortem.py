"""Postmortem drafting, and the triage agent embedded inside it."""

from langgraph.checkpoint.memory import InMemorySaver
from tests.unit.agents.conftest import TENANT, Harness

from paimon.agents.postmortem import (
    AGENT_NAME,
    REJECTED,
    SECTIONS,
    TIMELINE_DOCUMENT_ID,
    UNSUPPORTED,
    build_postmortem_graph,
    timeline_chunk,
)
from paimon.agents.triage import AGENT_NAME as TRIAGE_NAME
from paimon.domain.agents import AgentState, NodeSpec
from paimon.domain.entities import RunStatus
from paimon.infrastructure.orchestration import LangGraphWorkflow, build_serializer

TIMELINE = """09:00 drain started on node-7
09:12 eviction stalled
09:41 disruption budget relaxed, drain completed
"""


def graph(harness: Harness) -> object:
    return build_postmortem_graph(harness.collaborators())


def node(harness: Harness, name: str) -> NodeSpec:
    spec = build_postmortem_graph(harness.collaborators())
    return next(item for item in spec.nodes if item.name == name)


async def run(harness: Harness, question: str = TIMELINE) -> list[str]:
    return [
        step.name
        async for step in harness.workflow(graph(harness)).stream(
            question, thread_id="t-1", tenant_id=TENANT
        )
    ]


class TestTheTimelineAsASource:
    def test_it_becomes_a_citable_chunk_of_its_own_document(self) -> None:
        made = timeline_chunk(TIMELINE, TENANT)
        assert made.document_id == TIMELINE_DOCUMENT_ID
        assert made.text == TIMELINE

    def test_it_spans_the_whole_timeline_so_a_citation_resolves(self) -> None:
        made = timeline_chunk(TIMELINE, TENANT)
        assert made.start_char == 0
        assert made.end_char == len(TIMELINE)


class TestComposition:
    def test_the_triage_agent_appears_whole_under_its_prefix(self) -> None:
        names = [node.name for node in graph(Harness()).nodes]  # type: ignore[attr-defined]
        assert "precedent.frame" in names
        assert "precedent.procedure" in names
        assert "precedent.verify" in names

    def test_the_embedded_agent_is_not_its_own_run(self) -> None:
        # One flat graph, one run record. A nested runtime would have produced a
        # second run and hidden these steps from the parent's trace.
        spec = graph(Harness())
        assert spec.name == AGENT_NAME  # type: ignore[attr-defined]
        assert AGENT_NAME != TRIAGE_NAME

    def test_the_spliced_graph_validates(self) -> None:
        graph(Harness()).validate()  # type: ignore[attr-defined]


class TestGrounding:
    async def test_the_timeline_is_injected_after_triage_not_before(self) -> None:
        # Injecting it first would leave triage's own "is there anything here"
        # branch always answering yes, so the sub-agent would call a model on a
        # corpus that returned nothing.
        harness = Harness()
        names = await run(harness)
        assert names.index("ground") > names.index("precedent.assess")
        assert "precedent.refuse" in names
        assert "precedent.draft" not in names

    async def test_the_precedent_is_kept_before_the_draft_overwrites_it(self) -> None:
        harness = Harness()
        await harness.index()
        drafted = await node(harness, "precedent.draft").run(
            AgentState(
                question=TIMELINE,
                tenant_id=TENANT,
                evidence=(timeline_chunk(TIMELINE, TENANT),),
            )
        )
        kept = await node(harness, "ground").run(
            AgentState(question=TIMELINE, tenant_id=TENANT, draft=drafted["draft"])
        )
        assert kept["notes"] == drafted["draft"]

    async def test_a_postmortem_can_cite_the_timeline_itself(self) -> None:
        harness = Harness(answer="The drain stalled for forty minutes [1].")
        await harness.index()
        await run(harness)
        stored = await harness.checkpointer.load("t-1")
        assert stored is not None
        assert stored.status is RunStatus.SUCCEEDED
        assert stored.steps[-1].name == "verify"

    async def test_a_draft_citing_nothing_is_withdrawn(self) -> None:
        harness = Harness(answer="Everything was fine, no action needed.")
        state = AgentState(question=TIMELINE, tenant_id=TENANT, draft="unsupported", citations=())
        assert (await node(harness, "verify").run(state))["draft"] == UNSUPPORTED


class TestTheWholeAgent:
    async def test_it_runs_read_then_triage_then_compose(self) -> None:
        harness = Harness()
        await harness.index()
        names = await run(harness)
        assert names[0] == "read"
        assert names[-3:] == ["ground", "compose", "verify"]
        assert any(name.startswith("precedent.") for name in names)

    async def test_the_run_is_recorded_under_the_parent_agent(self) -> None:
        harness = Harness()
        await harness.index()
        await run(harness)
        stored = await harness.checkpointer.load("t-1")
        assert stored is not None
        assert stored.agent == AGENT_NAME

    async def test_the_prompt_names_every_section(self) -> None:
        harness = Harness()
        await harness.index()
        await run(harness)
        composed = harness.chat_model.calls[-1][-1].content
        for section in SECTIONS:
            assert section in composed


class TestSurvivingAReview:
    """A run suspended for a person, and what it still has when it continues.

    The one path in the platform where state crosses a checkpoint and comes back
    — minutes or hours later, possibly in a different process. Everything the
    run had gathered is reloaded from that checkpoint, so anything the encoding
    loses is lost here and nowhere else, which is why this is the test that
    proves the serializer's repair matters rather than merely works.
    """

    @staticmethod
    def _reviewable(harness: Harness) -> LangGraphWorkflow:
        return LangGraphWorkflow(
            build_postmortem_graph(harness.collaborators(), review=True),
            harness.checkpointer,
            saver=InMemorySaver(serde=build_serializer()),
        )

    async def test_a_resumed_run_keeps_the_citations_it_had(self) -> None:
        harness = Harness(answer="The drain stalled on a disruption budget [1].")
        await harness.index()
        workflow = self._reviewable(harness)

        async for _ in workflow.stream(TIMELINE, thread_id="t-review", tenant_id=TENANT):
            pass
        suspended = await harness.checkpointer.load("t-review")
        assert suspended is not None
        assert suspended.status is RunStatus.AWAITING_INPUT

        resumed = await workflow.resume("accept", thread_id="t-review")

        assert resumed.status is RunStatus.SUCCEEDED
        assert resumed.citations
        assert "[1]" in resumed.answer

    async def test_a_citation_that_crossed_the_checkpoint_still_resolves(self) -> None:
        # The offsets and the heading path are what a citation is *for*. A
        # heading path revived as a list rather than a tuple made a citation
        # unequal to the one that was stored, which is the difference between a
        # resumed run and a resumed run nothing downstream can match.
        harness = Harness(answer="The drain stalled on a disruption budget [1].")
        await harness.index()
        workflow = self._reviewable(harness)

        async for _ in workflow.stream(TIMELINE, thread_id="t-review", tenant_id=TENANT):
            pass
        resumed = await workflow.resume("accept", thread_id="t-review")

        citation = resumed.citations[0]
        assert isinstance(citation.heading_path, tuple)
        assert citation.end_char > citation.start_char
        assert citation.quote

    async def test_a_rejected_draft_comes_back_citing_nothing(self) -> None:
        # The other half: a reviewer who withdraws the draft leaves a run whose
        # record must not keep pointing at the sources of the text they refused.
        # The word matters — REVIEW_QUESTION asks for "reject: <reason>", and
        # anything else is reviewer notes on a draft that stands.
        harness = Harness(answer="The drain stalled on a disruption budget [1].")
        await harness.index()
        workflow = self._reviewable(harness)

        async for _ in workflow.stream(TIMELINE, thread_id="t-review", tenant_id=TENANT):
            pass
        resumed = await workflow.resume(
            "reject: this is wrong about the budget", thread_id="t-review"
        )

        assert resumed.citations == ()
        assert not resumed.grounded

    async def test_notes_are_not_a_rejection(self) -> None:
        # The distinction the review prompt draws, asserted because a reviewer
        # writing a correction and having the draft withdrawn is the kind of
        # surprise that stops people using a review step.
        harness = Harness(answer="The drain stalled on a disruption budget [1].")
        await harness.index()
        workflow = self._reviewable(harness)

        async for _ in workflow.stream(TIMELINE, thread_id="t-review", tenant_id=TENANT):
            pass
        resumed = await workflow.resume("tighten the wording in section two", thread_id="t-review")

        assert resumed.citations
        assert REJECTED not in resumed.answer
