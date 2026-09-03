"""Describing an agent's shape without naming an orchestration framework.

A :class:`GraphSpec` is data: named nodes, the edges between them, and the
branches that choose. The adapter in ``infrastructure.orchestration`` turns one
into a runnable graph (ADR-0015). Keeping the description here rather than in the
adapter means the shape of an agent can be inspected, validated and unit-tested
without a runtime, and it is what lets a node stay an ordinary async function.
"""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field

from paimon.domain.agents.state import AgentState, StateUpdate

#: Sentinel target meaning "this run is over". The adapter maps it onto whatever
#: its framework calls the terminal node; nothing here needs to know that name.
END = "__end__"

Node = Callable[[AgentState], Awaitable[StateUpdate]]


@dataclass(frozen=True, slots=True)
class StepReport:
    """What a node wants recorded about the work it just did.

    Separate from :class:`~paimon.agents.state.StateUpdate` because a step record
    is not state: it does not flow into the next node, it flows out to whoever is
    watching. Keeping the two apart is what stops nodes from having to time
    themselves — the adapter owns the clock, the node owns the meaning.
    """

    summary: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    details: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class NodeSpec:
    """One node: a name, the work, and how to describe what it did.

    Attributes:
        name: How this node appears in a run's trace. Must be unique in a graph.
        run: The work. A coroutine from a state to the fields it changed.
        summary: A fixed description, used when ``report`` is not given.
        report: Turns what the node saw and what it changed into a step record.
            Takes the input state as well as the update, because what is worth
            reporting is often about what arrived — a node that merges two
            branches changes nothing and has the most to say.
    """

    name: str
    run: Node
    summary: str = ""
    report: Callable[[AgentState, StateUpdate], StepReport] | None = None

    def describe(self, state: AgentState, update: StateUpdate) -> StepReport:
        """Return the step record for one execution of this node."""
        if self.report is not None:
            reported = self.report(state, update)
            if reported.summary:
                return reported
            return StepReport(
                summary=self.summary or self.name,
                input_tokens=reported.input_tokens,
                output_tokens=reported.output_tokens,
                details=reported.details,
            )
        return StepReport(summary=self.summary or self.name)


@dataclass(frozen=True, slots=True)
class Branch:
    """A conditional edge: one node, a decision, and where each answer leads.

    The decision is a plain function of the state, not a model call. That is the
    substance of ADR-0016: models decide content, code decides control flow, and
    a graph whose routing is a pure function is a graph that takes the same path
    twice for the same state.

    Attributes:
        source: The node the decision is taken after.
        decide: Returns one of the keys of ``targets``.
        targets: Maps each possible decision to a node name, or to :data:`END`.
    """

    source: str
    decide: Callable[[AgentState], str]
    targets: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class GraphSpec:
    """A complete agent, as data.

    Attributes:
        name: The agent's identifier, as it appears in a run record.
        entry: The node the run starts at.
        nodes: Every node, in no particular order.
        edges: Unconditional transitions, as ``(source, target)`` pairs. A target
            of :data:`END` finishes the run.
        branches: Conditional transitions.
    """

    name: str
    entry: str
    nodes: Sequence[NodeSpec]
    edges: Sequence[tuple[str, str]] = ()
    branches: Sequence[Branch] = ()

    def node_names(self) -> tuple[str, ...]:
        """Every node name, in declaration order."""
        return tuple(node.name for node in self.nodes)

    def validate(self) -> None:
        """Reject a graph that cannot run, before anything tries to run it.

        Every check here is one the orchestration framework would eventually
        raise for, but it would raise at compile time inside a stack trace full
        of framework frames. Raising here names the mistake in the vocabulary of
        the graph, and — because this needs no runtime — a unit test can prove
        each mistake is caught.

        Raises:
            ValueError: If the graph is unnamed, empty, refers to a node that
                does not exist, declares a name twice, or leaves a node that no
                path can reach.
        """
        if not self.name.strip():
            msg = "a graph must be named: the name identifies its runs"
            raise ValueError(msg)
        if not self.nodes:
            msg = f"graph '{self.name}' has no nodes"
            raise ValueError(msg)

        names = self.node_names()
        duplicates = {name for name in names if names.count(name) > 1}
        if duplicates:
            listed = ", ".join(sorted(duplicates))
            msg = f"graph '{self.name}' declares these node names twice: {listed}"
            raise ValueError(msg)

        known = set(names)
        if self.entry not in known:
            msg = f"graph '{self.name}' starts at '{self.entry}', which is not one of its nodes"
            raise ValueError(msg)

        for source, target in self.edges:
            self._require_known(source, known, "an edge starts at")
            if target != END:
                self._require_known(target, known, "an edge ends at")
        for branch in self.branches:
            self._require_known(branch.source, known, "a branch is taken after")
            if not branch.targets:
                msg = f"graph '{self.name}' has a branch after '{branch.source}' with no targets"
                raise ValueError(msg)
            for target in branch.targets.values():
                if target != END:
                    self._require_known(target, known, "a branch leads to")

        unreachable = known - self._reachable()
        if unreachable:
            listed = ", ".join(sorted(unreachable))
            # A node nothing reaches is either a routing mistake or dead code.
            # Both are worth failing for: the second costs nothing to delete, and
            # the first would otherwise show up as an agent that quietly skips a
            # step everyone assumed was running.
            msg = f"graph '{self.name}' can never reach these nodes: {listed}"
            raise ValueError(msg)

    def _require_known(self, name: str, known: set[str], context: str) -> None:
        if name not in known:
            msg = f"graph '{self.name}': {context} '{name}', which is not one of its nodes"
            raise ValueError(msg)

    def _reachable(self) -> set[str]:
        outgoing: dict[str, set[str]] = {name: set() for name in self.node_names()}
        for source, target in self.edges:
            if source in outgoing and target != END:
                outgoing[source].add(target)
        for branch in self.branches:
            if branch.source in outgoing:
                outgoing[branch.source].update(
                    target for target in branch.targets.values() if target != END
                )

        seen = {self.entry}
        frontier = [self.entry]
        while frontier:
            current = frontier.pop()
            for target in outgoing.get(current, ()):
                if target not in seen:
                    seen.add(target)
                    frontier.append(target)
        return seen
