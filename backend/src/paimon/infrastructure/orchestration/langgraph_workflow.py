"""Running a GraphSpec on LangGraph.

The whole of the platform's dependency on an orchestration framework lives in
this module (ADR-0015). It compiles the framework-free description in
``paimon.domain.agents`` into a ``StateGraph``, times each node, turns what the node
reports into an :class:`~paimon.domain.entities.AgentStep`, and streams those
steps out as they complete.
"""

import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, Protocol, cast

from langgraph.graph import END as LANGGRAPH_END
from langgraph.graph import START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from paimon.domain.agents import END, AgentState, GraphSpec, NodeSpec, StateUpdate
from paimon.domain.entities import AgentRun, AgentStep, RunStatus
from paimon.domain.errors import AgentRunError
from paimon.domain.ports import AgentCheckpointer

#: Guards against a graph that routes in a cycle forever. LangGraph's own default
#: is 25; naming it here makes it a decision rather than an inherited default.
DEFAULT_STEP_LIMIT = 25


class _CompiledNode(Protocol):
    """A node as LangGraph wants to receive it.

    Declared rather than expressed as ``Callable[[AgentState], ...]`` because the
    framework's own node protocol names its parameter ``state``, and a bare
    Callable has no parameter names — so the plain alias type-checks as
    incompatible for a reason that has nothing to do with behaviour.
    """

    async def __call__(self, state: AgentState) -> StateUpdate: ...


def _now() -> datetime:
    return datetime.now(UTC)


class LangGraphWorkflow:
    """An :class:`~paimon.domain.ports.AgentWorkflow` backed by LangGraph.

    One instance per agent. The compiled graph is built once at construction:
    compiling is pure work over the spec, and doing it per request would pay for
    it on every call and delay a structural error until the first run.
    """

    def __init__(
        self,
        spec: GraphSpec,
        checkpointer: AgentCheckpointer,
        *,
        step_limit: int = DEFAULT_STEP_LIMIT,
    ) -> None:
        """Compile a spec into a runnable graph.

        Args:
            spec: The agent's shape. Validated here, so a malformed graph fails
                at startup rather than on a caller's first question.
            checkpointer: Where runs are recorded.
            step_limit: Most nodes one run may execute.

        Raises:
            ValueError: If the spec does not describe a runnable graph.
        """
        spec.validate()
        self._spec = spec
        self._checkpointer = checkpointer
        self._step_limit = step_limit
        self._graph = self._compile(spec)

    @property
    def name(self) -> str:
        """The agent's identifier, as it appears in a run record."""
        return self._spec.name

    def _compile(self, spec: GraphSpec) -> CompiledStateGraph[AgentState, Any, Any, Any]:
        builder: StateGraph[AgentState, Any, Any, Any] = StateGraph(AgentState)
        for node in spec.nodes:
            builder.add_node(node.name, self._wrap(node))
        builder.add_edge(START, spec.entry)
        for source, target in spec.edges:
            builder.add_edge(source, LANGGRAPH_END if target == END else target)
        for branch in spec.branches:
            builder.add_conditional_edges(
                branch.source,
                branch.decide,
                {
                    decision: LANGGRAPH_END if target == END else target
                    for decision, target in branch.targets.items()
                },
            )
        return builder.compile()

    def _wrap(self, node: NodeSpec) -> _CompiledNode:
        """Turn a node into one that also records what it did.

        The timing lives here rather than in the node so that a node stays a pure
        function of its input: a node that reads the clock cannot be asserted on
        without freezing time.
        """

        async def execute(state: AgentState) -> StateUpdate:
            started_at = _now()
            began = time.perf_counter()
            try:
                update = await node.run(state)
            except Exception as error:  # noqa: BLE001
                # Deliberately blind. A node runs arbitrary adapter code, and
                # anything it raises has to become a recorded failure rather than
                # a framework traceback: narrowing this would let an unforeseen
                # error abort the run with no trace of how far it got, which is
                # the one outcome an operator cannot investigate.
                finished_at = _now()
                step = AgentStep(
                    name=node.name,
                    summary=f"failed: {error}",
                    started_at=started_at,
                    finished_at=finished_at,
                    details={"error": type(error).__name__},
                )
                return {"steps": (step,), "failure": f"{node.name}: {error}"}

            elapsed = time.perf_counter() - began
            report = node.describe(state, update)
            # A node that called a model reports its own share of the tokens in
            # the update; the reducer adds it to the run's running total, and it
            # is attributed here to the step that spent it. Without this the run
            # would total zero however many models it called, which is the kind
            # of number that is wrong in only one direction.
            spent = update.get("usage", (0, 0))
            step = AgentStep(
                name=node.name,
                summary=report.summary,
                started_at=started_at,
                # Derived from a monotonic clock rather than a second wall-clock
                # reading: a clock that steps backwards mid-node would otherwise
                # produce a step that finishes before it starts.
                finished_at=started_at.fromtimestamp(started_at.timestamp() + elapsed, tz=UTC),
                input_tokens=report.input_tokens or spent[0],
                output_tokens=report.output_tokens or spent[1],
                details=report.details,
            )
            existing = update.get("steps", ())
            return {**update, "steps": (*existing, step)}

        return execute

    async def stream(
        self, question: str, *, thread_id: str, tenant_id: str
    ) -> AsyncIterator[AgentStep]:
        """Run the graph, yielding each step as it completes.

        The run is checkpointed after every step, not only at the end. A process
        that dies mid-run then leaves a record of how far it got, which is the
        difference between a run that can be investigated and one that simply
        vanished.

        Raises:
            AgentRunError: If the graph could not be executed.
        """
        initial = AgentState(question=question, tenant_id=tenant_id)
        run = AgentRun(
            thread_id=thread_id,
            agent=self.name,
            tenant_id=tenant_id,
            status=RunStatus.RUNNING,
        )
        await self._checkpointer.save(run)

        seen: list[AgentStep] = []
        failure = ""
        try:
            async for chunk in self._graph.astream(
                initial,
                config={"recursion_limit": self._step_limit},
                stream_mode="updates",
            ):
                for update in cast("dict[str, StateUpdate]", chunk).values():
                    failure = update.get("failure", "") or failure
                    for step in update.get("steps", ()):
                        seen.append(step)
                        run = self._replace(run, RunStatus.RUNNING, seen)
                        await self._checkpointer.save(run)
                        yield step
        except Exception as error:
            await self._checkpointer.save(self._replace(run, RunStatus.FAILED, seen))
            msg = f"agent '{self.name}' could not complete run '{thread_id}': {error}"
            raise AgentRunError(msg) from error

        status = RunStatus.FAILED if failure else RunStatus.SUCCEEDED
        await self._checkpointer.save(self._replace(run, status, seen))

    @staticmethod
    def _replace(run: AgentRun, status: RunStatus, steps: list[AgentStep]) -> AgentRun:
        return AgentRun(
            thread_id=run.thread_id,
            agent=run.agent,
            tenant_id=run.tenant_id,
            status=status,
            steps=tuple(steps),
            started_at=run.started_at,
        )
