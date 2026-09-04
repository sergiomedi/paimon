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

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END as LANGGRAPH_END
from langgraph.graph import START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, interrupt

from paimon.domain.agents import END, AgentState, GraphSpec, NodeSpec, StateUpdate
from paimon.domain.entities import AgentRun, AgentStep, RunStatus
from paimon.domain.errors import AgentRunError, UnknownThreadError
from paimon.domain.ports import AgentCheckpointer

#: Guards against a graph that routes in a cycle forever. LangGraph's own default
#: is 25; naming it here makes it a decision rather than an inherited default.
DEFAULT_STEP_LIMIT = 25

#: The key an interrupt arrives under in an update stream. The framework's own
#: name for it, kept in one place so a rename is one edit rather than a hunt.
INTERRUPT_KEY = "__interrupt__"


def _question(raw: Any) -> str:
    """Read the question out of whatever the interrupt carried."""
    entries = raw if isinstance(raw, (list, tuple)) else [raw]
    for entry in entries:
        value = getattr(entry, "value", entry)
        if isinstance(value, dict) and value.get("question"):
            return str(value["question"])
        if isinstance(value, str) and value:
            return value
    return "a decision is required"


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
        saver: BaseCheckpointSaver[Any] | None = None,
        step_limit: int = DEFAULT_STEP_LIMIT,
    ) -> None:
        """Compile a spec into a runnable graph.

        Args:
            spec: The agent's shape. Validated here, so a malformed graph fails
                at startup rather than on a caller's first question.
            checkpointer: Where runs are recorded, as the domain describes them.
            saver: Where the graph's own state is kept, so a run suspended for a
                human decision can be resumed. A different thing from
                ``checkpointer`` and deliberately a separate argument
                (ADR-0017): one is this platform's record of what happened, the
                other is the framework's record of where it stopped. Without it
                the workflow runs normally and simply cannot be resumed.
            step_limit: Most nodes one run may execute.

        Raises:
            ValueError: If the spec does not describe a runnable graph.
        """
        spec.validate()
        self._spec = spec
        self._checkpointer = checkpointer
        self._step_limit = step_limit
        self._resumable = saver is not None
        self._graph = self._compile(spec, saver)

    @property
    def name(self) -> str:
        """The agent's identifier, as it appears in a run record."""
        return self._spec.name

    def _compile(
        self, spec: GraphSpec, saver: BaseCheckpointSaver[Any] | None
    ) -> CompiledStateGraph[AgentState, Any, Any, Any]:
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
        return builder.compile(checkpointer=saver)

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

            if awaiting := update.get("awaiting", ""):
                # The node asked for a person. Interrupting is done here rather
                # than in the node because it is a property of the runtime: a
                # node that called interrupt() would need a graph to be tested,
                # which is the whole thing ADR-0015 bought.
                #
                # interrupt() raises the first time and returns the answer when
                # the run is resumed, so everything above this line runs twice.
                # Node bodies are pure, so that is wasteful rather than wrong -
                # but a node that calls a model should not be the one to suspend.
                decision = interrupt({"question": awaiting, "node": node.name})
                update = {**update, "awaiting": "", "decision": str(decision)}

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

    def _config(self, thread_id: str) -> RunnableConfig:
        return cast(
            "RunnableConfig",
            {
                "recursion_limit": self._step_limit,
                # Addresses the graph's own checkpoint. It is the same thread the
                # platform records the run under, deliberately: two identifiers
                # for one run is one identifier too many for whoever has to
                # correlate them at three in the morning.
                "configurable": {"thread_id": thread_id},
            },
        )

    async def _drain(
        self, graph_input: Any, run: AgentRun, thread_id: str
    ) -> AsyncIterator[AgentStep]:
        """Advance the graph, yielding steps and keeping the run record current.

        Shared by starting and resuming, because the two differ only in what is
        handed to the graph: a fresh state, or a command carrying a decision.
        """
        seen: list[AgentStep] = list(run.steps)
        answer = run.answer
        failure = ""
        suspended = ""
        try:
            async for chunk in self._graph.astream(
                graph_input, config=self._config(thread_id), stream_mode="updates"
            ):
                for key, value in cast("dict[str, Any]", chunk).items():
                    if key == INTERRUPT_KEY:
                        suspended = _question(value)
                        continue
                    update = cast("StateUpdate", value)
                    failure = update.get("failure", "") or failure
                    # The last node to write a draft owns the answer. Nodes that
                    # withdraw one write it too, so a withdrawal replaces the
                    # draft it withdrew rather than leaving both on the record.
                    answer = update.get("draft", "") or answer
                    for step in update.get("steps", ()):
                        seen.append(step)
                        run = self._replace(run, RunStatus.RUNNING, seen, answer)
                        await self._checkpointer.save(run)
                        yield step
        except Exception as error:
            await self._checkpointer.save(self._replace(run, RunStatus.FAILED, seen, answer))
            msg = f"agent '{self.name}' could not complete run '{thread_id}': {error}"
            raise AgentRunError(msg) from error

        if suspended:
            # Not a success and not a failure. The run is waiting for a person,
            # and an operator who cannot see that state will never answer.
            status = RunStatus.AWAITING_INPUT
        else:
            status = RunStatus.FAILED if failure else RunStatus.SUCCEEDED
        await self._checkpointer.save(self._replace(run, status, seen, answer))

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
        run = AgentRun(
            thread_id=thread_id,
            agent=self.name,
            tenant_id=tenant_id,
            status=RunStatus.RUNNING,
        )
        await self._checkpointer.save(run)
        async for step in self._drain(
            AgentState(question=question, tenant_id=tenant_id), run, thread_id
        ):
            yield step

    async def resume(self, decision: str, *, thread_id: str) -> AgentRun:
        """Continue a run that stopped for a human decision.

        Args:
            decision: What the person decided.
            thread_id: The suspended run.

        Returns:
            The run after it has progressed.

        Raises:
            UnknownThreadError: If no such run was ever checkpointed, or this
                deployment kept no graph state to resume from.
            AgentRunError: If the run could not be continued.
        """
        if not self._resumable:
            msg = (
                f"run '{thread_id}' cannot be resumed: this deployment runs "
                "without graph checkpointing"
            )
            raise UnknownThreadError(msg)

        run = await self._checkpointer.load(thread_id)
        if run is None:
            msg = f"no run '{thread_id}' to resume"
            raise UnknownThreadError(msg)

        async for _ in self._drain(Command(resume=decision), run, thread_id):
            pass
        resumed = await self._checkpointer.load(thread_id)
        if resumed is None:  # pragma: no cover - the drain just saved it
            msg = f"run '{thread_id}' disappeared while being resumed"
            raise UnknownThreadError(msg)
        return resumed

    @staticmethod
    def _replace(run: AgentRun, status: RunStatus, steps: list[AgentStep], answer: str) -> AgentRun:
        return AgentRun(
            thread_id=run.thread_id,
            agent=run.agent,
            tenant_id=run.tenant_id,
            status=status,
            answer=answer,
            steps=tuple(steps),
            started_at=run.started_at,
        )
