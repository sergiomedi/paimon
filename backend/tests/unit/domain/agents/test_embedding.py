"""Splicing one agent into another, on the description alone."""

import pytest

from paimon.domain.agents import END, AgentState, Branch, GraphSpec, NodeSpec, StateUpdate, embed


async def noop(state: AgentState) -> StateUpdate:
    return {}


def sub() -> GraphSpec:
    return GraphSpec(
        name="triage",
        entry="retrieve",
        nodes=[
            NodeSpec(name="retrieve", run=noop, summary="looked"),
            NodeSpec(name="answer", run=noop),
            NodeSpec(name="refuse", run=noop),
        ],
        edges=[("answer", END), ("refuse", END)],
        branches=[
            Branch(
                source="retrieve",
                decide=lambda state: "answer" if state.evidence else "refuse",
                targets={"answer": "answer", "refuse": "refuse"},
            )
        ],
    )


class TestEmbedding:
    def test_every_node_is_renamed_under_the_prefix(self) -> None:
        embedded = embed(sub(), "precedent", exit_to="compose")
        assert [node.name for node in embedded.nodes] == [
            "precedent.retrieve",
            "precedent.answer",
            "precedent.refuse",
        ]

    def test_the_entry_is_renamed_too(self) -> None:
        assert embed(sub(), "precedent", exit_to="compose").entry == "precedent.retrieve"

    def test_what_ended_the_sub_agent_now_continues_the_parent(self) -> None:
        embedded = embed(sub(), "precedent", exit_to="compose")
        assert set(embedded.edges) == {
            ("precedent.answer", "compose"),
            ("precedent.refuse", "compose"),
        }

    def test_branch_targets_are_rewritten(self) -> None:
        branch = embed(sub(), "precedent", exit_to="compose").branches[0]
        assert branch.source == "precedent.retrieve"
        assert branch.targets == {
            "answer": "precedent.answer",
            "refuse": "precedent.refuse",
        }

    def test_the_decision_function_is_carried_over_untouched(self) -> None:
        branch = embed(sub(), "precedent", exit_to="compose").branches[0]
        empty = AgentState(question="why?", tenant_id="tenant-a")
        assert branch.decide(empty) == "refuse"

    def test_a_node_keeps_its_summary_and_the_very_same_body(self) -> None:
        # Identity, not equality: embedding renames a node, it does not wrap it.
        # A wrapper here would be a second place for behaviour to diverge.
        original = sub()
        node = embed(original, "precedent", exit_to="compose").nodes[0]
        assert node.summary == "looked"
        assert node.run is original.nodes[0].run

    def test_two_embeddings_of_one_agent_do_not_collide(self) -> None:
        # The reason names are prefixed rather than reused: an agent embedded
        # twice would otherwise declare each node name twice, and validation
        # would reject the parent for a mistake the parent did not make.
        first = embed(sub(), "precedent", exit_to="compose")
        second = embed(sub(), "similar", exit_to="compose")
        assert {node.name for node in first.nodes}.isdisjoint(node.name for node in second.nodes)

    def test_a_blank_prefix_is_refused(self) -> None:
        with pytest.raises(ValueError, match="needs a prefix"):
            embed(sub(), "  ", exit_to="compose")

    def test_a_broken_sub_agent_is_rejected_before_it_is_spliced(self) -> None:
        # Otherwise the parent fails validation for a mistake made elsewhere,
        # and the error names the wrong graph.
        broken = GraphSpec(name="triage", entry="nowhere", nodes=[NodeSpec("answer", noop)])
        with pytest.raises(ValueError, match="graph 'triage' starts at 'nowhere'"):
            embed(broken, "precedent", exit_to="compose")

    def test_the_spliced_parent_validates_as_one_flat_graph(self) -> None:
        embedded = embed(sub(), "precedent", exit_to="compose")
        GraphSpec(
            name="postmortem",
            entry="read",
            nodes=[
                NodeSpec(name="read", run=noop),
                *embedded.nodes,
                NodeSpec(name="compose", run=noop),
            ],
            edges=[("read", embedded.entry), *embedded.edges, ("compose", END)],
            branches=list(embedded.branches),
        ).validate()
