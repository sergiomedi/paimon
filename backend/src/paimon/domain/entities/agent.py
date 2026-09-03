"""Agent runs and the steps they are made of.

A run is the unit the platform persists, resumes and explains. Its steps are an
append-only record of what happened, not a log: they are how a caller sees what
an agent did and how a human decides whether to let it continue (ADR-0016).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum


class RunStatus(StrEnum):
    """Where a run has got to.

    ``AWAITING_INPUT`` is not a failure and not a success: it is a run suspended
    on purpose at a point where a person has to decide. Collapsing it into
    ``RUNNING`` would hide the one state an operator has to act on.
    """

    RUNNING = "running"
    AWAITING_INPUT = "awaiting_input"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class AgentStep:
    """One completed node of a run.

    Attributes:
        name: The node that produced this step.
        summary: What the node did, in one line, for a human reading the trace.
        started_at: When the node began.
        finished_at: When it ended.
        input_tokens: Tokens the node's model calls consumed, zero if none.
        output_tokens: Tokens the node's model calls produced, zero if none.
        details: Node-specific facts worth surfacing, as strings so the step
            survives any store and never carries a live object.
    """

    name: str
    summary: str
    started_at: datetime
    finished_at: datetime
    input_tokens: int = 0
    output_tokens: int = 0
    details: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Reject a step that cannot be placed in time or attributed.

        Raises:
            ValueError: If the node is unnamed, the timestamps are naive or run
                backwards, or a token count is negative.
        """
        if not self.name.strip():
            msg = "a step must name the node that produced it"
            raise ValueError(msg)
        for moment in (self.started_at, self.finished_at):
            if moment.tzinfo is None:
                msg = "step timestamps must be timezone-aware"
                raise ValueError(msg)
        if self.finished_at < self.started_at:
            msg = "a step cannot finish before it starts"
            raise ValueError(msg)
        if self.input_tokens < 0 or self.output_tokens < 0:
            msg = "token counts cannot be negative"
            raise ValueError(msg)

    @property
    def duration_ms(self) -> float:
        """How long the node took, in milliseconds."""
        return (self.finished_at - self.started_at).total_seconds() * 1000

    @property
    def total_tokens(self) -> int:
        """Tokens this step consumed and produced."""
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True, slots=True)
class AgentRun:
    """One execution of one agent, as the outside world sees it.

    Identified by ``thread_id`` rather than by a surrogate key: resuming a
    suspended run means addressing the same thread, and a run that cannot be
    named cannot be resumed.
    """

    thread_id: str
    agent: str
    tenant_id: str
    status: RunStatus
    steps: Sequence[AgentStep] = ()
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        """Reject a run that cannot be addressed.

        Raises:
            ValueError: If the thread, agent or tenant is blank.
        """
        for name in ("thread_id", "agent", "tenant_id"):
            if not str(getattr(self, name)).strip():
                msg = f"a run requires a non-empty {name}"
                raise ValueError(msg)

    @property
    def total_tokens(self) -> int:
        """What the run has cost so far."""
        return sum(step.total_tokens for step in self.steps)

    @property
    def is_terminal(self) -> bool:
        """Whether the run has reached a state it will not leave on its own."""
        return self.status in (RunStatus.SUCCEEDED, RunStatus.FAILED)
