"""Request and response models for agent runs."""

from datetime import datetime

from pydantic import BaseModel, Field

from paimon.domain.entities import AgentRun, AgentStep


class AgentSummaryResponse(BaseModel):
    """One agent the platform offers."""

    name: str
    description: str


class StartRunRequest(BaseModel):
    """What an agent is being asked to work on."""

    input: str = Field(
        min_length=1,
        max_length=20000,
        description="The symptom, timeline or topic, depending on the agent.",
    )


class AgentStepResponse(BaseModel):
    """One completed node of a run.

    Emitted as it happens, so a client can show progress rather than a spinner.
    """

    name: str
    summary: str
    started_at: datetime
    finished_at: datetime
    duration_ms: float
    input_tokens: int
    output_tokens: int
    details: dict[str, str]

    @classmethod
    def from_step(cls, step: AgentStep) -> "AgentStepResponse":
        """Build the response from the domain entity."""
        return cls(
            name=step.name,
            summary=step.summary,
            started_at=step.started_at,
            finished_at=step.finished_at,
            duration_ms=step.duration_ms,
            input_tokens=step.input_tokens,
            output_tokens=step.output_tokens,
            details=dict(step.details),
        )


class AgentRunResponse(BaseModel):
    """A run, as it stands.

    ``total_tokens`` is on the run rather than left to the caller to add up:
    cost per run is the number anyone asks for first.
    """

    thread_id: str
    agent: str
    status: str
    started_at: datetime
    total_tokens: int
    steps: list[AgentStepResponse]

    @classmethod
    def from_run(cls, run: AgentRun) -> "AgentRunResponse":
        """Build the response from the domain entity."""
        return cls(
            thread_id=run.thread_id,
            agent=run.agent,
            status=str(run.status),
            started_at=run.started_at,
            total_tokens=run.total_tokens,
            steps=[AgentStepResponse.from_step(step) for step in run.steps],
        )


class DecisionRequest(BaseModel):
    """What a person decided about a suspended run."""

    decision: str = Field(
        min_length=1,
        max_length=4000,
        description="The answer to the question the run stopped on.",
    )


class AgentRunListResponse(BaseModel):
    """A tenant's recent runs, most recently started first."""

    runs: list[AgentRunResponse]
