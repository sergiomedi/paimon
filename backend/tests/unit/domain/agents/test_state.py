"""The state reducers, which decide what parallel branches do to a run's record."""

from dataclasses import fields
from datetime import UTC, datetime, timedelta

from paimon.domain.agents import AgentState, StateUpdate, append_steps, merge_evidence
from paimon.domain.entities import AgentStep, Chunk
from paimon.domain.value_objects import Citation

BEGAN = datetime(2026, 9, 3, 9, 0, tzinfo=UTC)


def step(name: str) -> AgentStep:
    return AgentStep(
        name=name, summary=f"{name} ran", started_at=BEGAN, finished_at=BEGAN + timedelta(seconds=1)
    )


def chunk(chunk_id: str, text: str = "a passage") -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        document_id="doc-1",
        tenant_id="tenant-a",
        ordinal=0,
        text=text,
        start_char=0,
        end_char=len(text),
        token_count=2,
    )


class TestAppendSteps:
    def test_it_concatenates_rather_than_replacing(self) -> None:
        assert [item.name for item in append_steps((step("a"),), (step("b"),))] == ["a", "b"]

    def test_a_branch_that_recorded_nothing_erases_nothing(self) -> None:
        assert [item.name for item in append_steps((step("a"),), ())] == ["a"]


class TestMergeEvidence:
    def test_it_keeps_both_branches(self) -> None:
        merged = merge_evidence((chunk("c-1"),), (chunk("c-2"),))
        assert [item.chunk_id for item in merged] == ["c-1", "c-2"]

    def test_a_chunk_both_branches_found_appears_once(self) -> None:
        merged = merge_evidence((chunk("c-1"), chunk("c-2")), (chunk("c-2"), chunk("c-3")))
        assert [item.chunk_id for item in merged] == ["c-1", "c-2", "c-3"]

    def test_the_first_occurrence_wins_so_the_merge_is_deterministic(self) -> None:
        merged = merge_evidence((chunk("c-1", "left"),), (chunk("c-1", "right"),))
        assert [item.text for item in merged] == ["left"]


class TestAgentState:
    def test_a_state_with_no_citations_is_not_grounded(self) -> None:
        state = AgentState(question="why?", tenant_id="tenant-a", draft="an answer")
        assert not state.grounded

    def test_a_state_is_grounded_once_it_cites_something(self) -> None:
        citation = Citation(
            marker=1,
            document_id="doc-1",
            chunk_id="c-1",
            source_uri="https://example.test/doc",
            title="A document",
            heading_path=("Title",),
            start_char=0,
            end_char=9,
            quote="a passage",
        )
        state = AgentState(question="why?", tenant_id="tenant-a", citations=(citation,))
        assert state.grounded


def test_the_update_type_covers_exactly_the_state_fields() -> None:
    """A drift guard, and the reason StateUpdate is worth having at all.

    A node returning a key that is not a state field has that key silently
    discarded at runtime. ``total=False`` makes the type checker reject an
    unknown key, but only while the two definitions agree — so the agreement is
    asserted here rather than maintained by memory.
    """
    assert set(StateUpdate.__annotations__) == {field.name for field in fields(AgentState)}
