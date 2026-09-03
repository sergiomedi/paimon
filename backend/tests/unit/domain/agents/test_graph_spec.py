"""The graph description validates itself, with no runtime involved.

Every case here is a mistake the orchestration framework would also catch — but
it would catch it at compile time, inside framework frames, phrased in framework
vocabulary. These tests exist because a graph that explains its own breakage is
worth more than one that defers the explanation.
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


async def noop(state: AgentState) -> StateUpdate:
    return {}


def node(name: str, **overrides: object) -> NodeSpec:
    values: dict[str, object] = {"name": name, "run": noop}
    values.update(overrides)
    return NodeSpec(**values)  # type: ignore[arg-type]


def spec(**overrides: object) -> GraphSpec:
    values: dict[str, object] = {
        "name": "triage",
        "entry": "retrieve",
        "nodes": [node("retrieve"), node("answer")],
        "edges": [("retrieve", "answer"), ("answer", END)],
    }
    values.update(overrides)
    return GraphSpec(**values)  # type: ignore[arg-type]


class TestValidation:
    def test_a_well_formed_graph_validates(self) -> None:
        spec().validate()

    def test_an_unnamed_graph_is_refused(self) -> None:
        with pytest.raises(ValueError, match="must be named"):
            spec(name="  ").validate()

    def test_an_empty_graph_is_refused(self) -> None:
        with pytest.raises(ValueError, match="has no nodes"):
            spec(nodes=[], entry="retrieve").validate()

    def test_a_repeated_node_name_is_refused(self) -> None:
        # Two nodes of one name means one silently replaces the other, and the
        # run skips a step nobody notices is missing.
        with pytest.raises(ValueError, match="node names twice: answer"):
            spec(nodes=[node("retrieve"), node("answer"), node("answer")]).validate()

    def test_an_entry_that_is_not_a_node_is_refused(self) -> None:
        with pytest.raises(ValueError, match="starts at 'nowhere'"):
            spec(entry="nowhere").validate()

    def test_an_edge_to_an_unknown_node_is_refused(self) -> None:
        with pytest.raises(ValueError, match="an edge ends at 'nowhere'"):
            spec(edges=[("retrieve", "nowhere"), ("answer", END)]).validate()

    def test_an_edge_from_an_unknown_node_is_refused(self) -> None:
        with pytest.raises(ValueError, match="an edge starts at 'nowhere'"):
            spec(edges=[("retrieve", "answer"), ("nowhere", END)]).validate()

    def test_an_unreachable_node_is_refused(self) -> None:
        with pytest.raises(ValueError, match="can never reach these nodes: orphan"):
            spec(nodes=[node("retrieve"), node("answer"), node("orphan")]).validate()

    def test_a_branch_with_no_targets_is_refused(self) -> None:
        with pytest.raises(ValueError, match="branch after 'retrieve' with no targets"):
            spec(
                edges=[("answer", END)],
                branches=[Branch(source="retrieve", decide=lambda _: "go", targets={})],
            ).validate()

    def test_a_branch_leading_nowhere_is_refused(self) -> None:
        with pytest.raises(ValueError, match="a branch leads to 'nowhere'"):
            spec(
                edges=[("answer", END)],
                branches=[
                    Branch(source="retrieve", decide=lambda _: "go", targets={"go": "nowhere"})
                ],
            ).validate()

    def test_a_node_reached_only_by_a_branch_counts_as_reachable(self) -> None:
        spec(
            nodes=[node("retrieve"), node("answer"), node("refuse")],
            edges=[("answer", END), ("refuse", END)],
            branches=[
                Branch(
                    source="retrieve",
                    decide=lambda state: "answer" if state.evidence else "refuse",
                    targets={"answer": "answer", "refuse": "refuse"},
                )
            ],
        ).validate()


class TestNodeDescription:
    def test_a_node_without_a_report_uses_its_fixed_summary(self) -> None:
        assert node("retrieve", summary="looked for material").describe({}).summary == (
            "looked for material"
        )

    def test_a_node_with_neither_falls_back_to_its_name(self) -> None:
        assert node("retrieve").describe({}).summary == "retrieve"

    def test_a_report_can_describe_what_actually_happened(self) -> None:
        described = node(
            "retrieve",
            summary="looked for material",
            report=lambda update: StepReport(
                summary=f"retrieved {len(update.get('evidence', ()))} chunks"
            ),
        ).describe({"evidence": ()})
        assert described.summary == "retrieved 0 chunks"

    def test_a_report_that_only_counts_tokens_keeps_the_fixed_summary(self) -> None:
        described = node(
            "answer",
            summary="drafted an answer",
            report=lambda _: StepReport(input_tokens=80, output_tokens=20),
        ).describe({})
        assert described.summary == "drafted an answer"
        assert described.input_tokens == 80
