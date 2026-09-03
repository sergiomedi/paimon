"""Endpoints for starting agent runs and reading what they did."""

import json
import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import StreamingResponse

from paimon.agents import AGENT_DESCRIPTIONS
from paimon.domain.entities import RunStatus
from paimon.domain.errors import AgentRunError
from paimon.domain.ports import AgentWorkflow, HumanInTheLoop
from paimon.interfaces.api.dependencies import (
    AgentCheckpointerDep,
    AgentWorkflowsDep,
    CurrentPrincipal,
)
from paimon.interfaces.api.schemas import ErrorResponse
from paimon.interfaces.api.schemas_agents import (
    AgentRunListResponse,
    AgentRunResponse,
    AgentStepResponse,
    AgentSummaryResponse,
    DecisionRequest,
    StartRunRequest,
)

router = APIRouter(prefix="/agents", tags=["agents"])

#: One JSON object per line. Chosen over server-sent events because a run is a
#: sequence of records rather than a UI notification stream: NDJSON is readable
#: with curl, parseable by a two-line client, and survives a proxy that knows
#: nothing about SSE framing.
NDJSON = "application/x-ndjson"


@router.get("", response_model=list[AgentSummaryResponse], summary="List the available agents")
async def list_agents(
    principal: CurrentPrincipal, workflows: AgentWorkflowsDep
) -> list[AgentSummaryResponse]:
    """Return every agent this deployment offers.

    Authenticated like everything else. Which agents a deployment runs is a
    description of what it can do, and an endpoint that enumerates capabilities
    to anyone who asks is a disclosure whether or not it returns data.
    """
    _ = principal
    return [
        AgentSummaryResponse(name=name, description=AGENT_DESCRIPTIONS.get(name, ""))
        for name in sorted(workflows)
    ]


def _workflow(workflows: dict[str, AgentWorkflow], name: str) -> AgentWorkflow:
    workflow = workflows.get(name)
    if workflow is None:
        known = ", ".join(sorted(workflows))
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no agent named '{name}'; this deployment offers: {known}",
        )
    return workflow


@router.post(
    "/{agent}/runs",
    status_code=status.HTTP_200_OK,
    summary="Start a run and stream its steps",
    response_class=StreamingResponse,
    responses={
        status.HTTP_404_NOT_FOUND: {"model": ErrorResponse},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorResponse},
    },
)
async def start_run(
    agent: str,
    request: StartRunRequest,
    principal: CurrentPrincipal,
    workflows: AgentWorkflowsDep,
) -> StreamingResponse:
    """Run an agent, emitting each step as it completes.

    Streaming rather than returning the finished run: an agent takes several
    seconds and several model calls, and a caller who can watch it can also stop
    reading. The thread id is returned in a header so the run can be read back
    later even if the client disconnects mid-stream — the steps are checkpointed
    as they happen, so a dropped connection loses the stream, not the record.
    """
    workflow = _workflow(workflows, agent)
    thread_id = str(uuid.uuid4())

    async def stream() -> AsyncIterator[bytes]:
        try:
            async for step in workflow.stream(
                request.input, thread_id=thread_id, tenant_id=principal.tenant_id
            ):
                payload = AgentStepResponse.from_step(step).model_dump(mode="json")
                yield (json.dumps(payload) + "\n").encode()
        except AgentRunError as error:
            # The response has already begun, so the status code is spent. The
            # failure is reported as a final record instead, and it is also on
            # the checkpointed run: a client that missed it can still find out.
            yield (json.dumps({"error": str(error)}) + "\n").encode()

    return StreamingResponse(
        stream(),
        media_type=NDJSON,
        headers={"X-Paimon-Thread-Id": thread_id},
    )


@router.get(
    "/runs",
    response_model=AgentRunListResponse,
    summary="List this tenant's recent runs",
)
async def list_runs(
    principal: CurrentPrincipal,
    checkpointer: AgentCheckpointerDep,
    limit: int = 50,
) -> AgentRunListResponse:
    """Return recent runs, most recently started first."""
    runs = await checkpointer.list_runs(principal.tenant_id, limit=min(max(limit, 1), 200))
    return AgentRunListResponse(runs=[AgentRunResponse.from_run(run) for run in runs])


@router.post(
    "/runs/{thread_id}/decision",
    response_model=AgentRunResponse,
    summary="Answer a run that is waiting for a person",
    responses={
        status.HTTP_404_NOT_FOUND: {"model": ErrorResponse},
        status.HTTP_409_CONFLICT: {"model": ErrorResponse},
    },
)
async def decide(
    thread_id: str,
    request: DecisionRequest,
    principal: CurrentPrincipal,
    checkpointer: AgentCheckpointerDep,
    workflows: AgentWorkflowsDep,
) -> AgentRunResponse:
    """Continue a suspended run with a person's answer.

    Returns the finished run rather than streaming, because what a reviewer wants
    after answering is the outcome, and the remaining steps are on the record
    either way.
    """
    run = await checkpointer.load(thread_id)
    if run is None or run.tenant_id != principal.tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"no run '{thread_id}'")
    if run.status is not RunStatus.AWAITING_INPUT:
        # A run that is still going, or already over, has no question open. This
        # is a conflict rather than a bad request: the request was well formed
        # and the run was simply not in a state to receive it.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"run '{thread_id}' is {run.status}, not waiting for a decision",
        )

    workflow = _workflow(workflows, run.agent)
    if not isinstance(workflow, HumanInTheLoop):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"agent '{run.agent}' cannot be resumed in this deployment",
        )
    return AgentRunResponse.from_run(await workflow.resume(request.decision, thread_id=thread_id))


@router.get(
    "/runs/{thread_id}",
    response_model=AgentRunResponse,
    summary="Read one run",
    responses={status.HTTP_404_NOT_FOUND: {"model": ErrorResponse}},
)
async def read_run(
    thread_id: str,
    principal: CurrentPrincipal,
    checkpointer: AgentCheckpointerDep,
) -> AgentRunResponse:
    """Return a run of this tenant's.

    A run belonging to another tenant is reported as absent rather than as
    forbidden: telling a caller that a thread exists but is not theirs is itself
    a disclosure, and there is nothing they can do with the answer.
    """
    run = await checkpointer.load(thread_id)
    if run is None or run.tenant_id != principal.tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"no run '{thread_id}'")
    return AgentRunResponse.from_run(run)
