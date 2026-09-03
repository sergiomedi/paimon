"""Ports for running agent workflows and remembering what they did."""

from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Protocol, runtime_checkable

from paimon.domain.entities import AgentRun, AgentStep


@runtime_checkable
class AgentWorkflow(Protocol):
    """One runnable agent, addressed by thread.

    Deliberately narrow. The application needs to start a run, watch it, resume a
    suspended one and read back what happened; it does not need to know that a
    graph exists, which is what keeps the orchestration framework replaceable and
    the use cases testable without it.
    """

    @property
    def name(self) -> str:
        """Identifier of this workflow, as it appears in a run record."""
        ...

    def stream(self, question: str, *, thread_id: str, tenant_id: str) -> AsyncIterator[AgentStep]:
        """Run the workflow, yielding each step as it completes.

        Streaming rather than returning a finished run because a step that is
        only visible after the last one has finished cannot be watched, and
        watching is the point: an operator decides whether to let a run continue.

        Args:
            question: What the agent is being asked.
            thread_id: Identifies this run, and addresses it for resumption.
            tenant_id: Whose material the run may read.

        Yields:
            Each completed step, in order.

        Raises:
            AgentRunError: If the run could not be completed.
        """
        ...


@runtime_checkable
class HumanInTheLoop(Protocol):
    """A workflow that can stop for a person and continue afterwards.

    A capability, not part of :class:`AgentWorkflow`, for the reason ADR-0014
    gave for :class:`~paimon.domain.ports.NativeHybridSearch`: not every workflow
    has a decision worth interrupting for, and requiring ``resume`` of all of
    them would force most to implement a method that only ever raises. A
    capability expressed as a protocol is one the type checker can ask about; a
    capability expressed as a boolean is one every caller has to remember to.
    """

    async def resume(self, decision: str, *, thread_id: str) -> AgentRun:
        """Continue a run that stopped for a human decision.

        Args:
            decision: What the person decided.
            thread_id: The suspended run.

        Returns:
            The run after it has progressed.

        Raises:
            UnknownThreadError: If no such run was ever checkpointed.
            AgentRunError: If the run could not be continued.
        """
        ...


@runtime_checkable
class AgentCheckpointer(Protocol):
    """Persists a run so it can be inspected and resumed.

    Separate from :class:`AgentWorkflow` because durability is not a property of
    any one agent: every workflow wants it, and a deployment may want it backed
    by memory in tests and by PostgreSQL in production without either workflow
    changing.
    """

    async def save(self, run: AgentRun) -> None:
        """Record the current state of a run, replacing any earlier state.

        Raises:
            CheckpointError: If the run could not be persisted.
        """
        ...

    async def load(self, thread_id: str) -> AgentRun | None:
        """Return a run, or None when the thread is unknown.

        None rather than an exception: asking after a run that was never started
        is an ordinary question with an ordinary answer.

        Raises:
            CheckpointError: If the store could not be read.
        """
        ...

    async def list_runs(self, tenant_id: str, *, limit: int = 50) -> Sequence[AgentRun]:
        """Return recent runs for a tenant, most recently started first.

        Raises:
            CheckpointError: If the store could not be read.
        """
        ...


@runtime_checkable
class AgentMemory(Protocol):
    """What an agent recalls across runs, as opposed to within one.

    Distinct from the checkpointer on purpose, and the distinction is the one
    LangGraph draws between a thread-scoped checkpoint and a cross-thread store:
    a checkpoint is *this* run's history, memory is what earlier runs learned
    that this one should benefit from. Conflating them produces a store that
    grows without bound and is recalled by nobody.
    """

    async def remember(self, namespace: Sequence[str], key: str, value: Mapping[str, str]) -> None:
        """Write a memory under a namespace.

        Raises:
            AgentMemoryError: If the memory could not be written.
        """
        ...

    async def recall(
        self, namespace: Sequence[str], query: str, *, limit: int = 5
    ) -> Sequence[Mapping[str, str]]:
        """Return memories in a namespace most relevant to a query.

        Raises:
            AgentMemoryError: If the store could not be searched.
        """
        ...
