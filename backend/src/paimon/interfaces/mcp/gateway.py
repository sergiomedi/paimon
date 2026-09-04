"""Turning an MCP tool call into an authenticated, tenant-scoped execution.

Separate from the server so the decision that matters — who is calling, and
whose material they may read — is a plain class with plain tests, rather than
something only reachable through a protocol session.
"""

import uuid
from collections.abc import Callable, Mapping

from paimon.agents.tools import ToolExecutor
from paimon.application.use_cases.retrieve_chunks import RetrieveChunks
from paimon.domain.entities import AgentRun, Principal
from paimon.domain.errors import (
    AgentRunError,
    AuthenticationError,
    DomainError,
    InvalidTokenError,
)
from paimon.domain.ports import (
    AgentCheckpointer,
    AgentWorkflow,
    DocumentRepository,
    IdentityProvider,
    ToolCall,
)
from paimon.observability.genai import (
    TENANT,
    TOOL_CALL_ID,
    TOOL_NAME,
    TOOL_TYPE,
    Operation,
    operation_span,
)
from paimon.observability.recording import measured_tool

BEARER = "bearer"


class UnknownAgentError(DomainError):
    """A client asked for an agent this deployment does not offer."""


def bearer_token(headers: Mapping[str, str] | None) -> str:
    """Extract the bearer token from request headers.

    Raises:
        InvalidTokenError: If there is no usable Authorization header. Headers
            are client-supplied input, so a missing or malformed one is an
            ordinary outcome rather than an internal error.
    """
    if not headers:
        # stdio transports carry no headers. This platform's server is HTTP-only
        # precisely because it has a tenant to establish, and a transport that
        # cannot carry a token cannot establish one.
        msg = "this MCP server requires an Authorization header, which this transport did not carry"
        raise InvalidTokenError(msg)

    raw = headers.get("authorization") or headers.get("Authorization") or ""
    scheme, _, token = raw.partition(" ")
    if scheme.strip().lower() != BEARER or not token.strip():
        msg = "missing or malformed Authorization header"
        raise InvalidTokenError(msg)
    return token.strip()


class McpToolGateway:
    """Authenticates an MCP caller and runs the tool they asked for.

    Every call is authenticated separately. The 2026-07-28 protocol is stateless
    — there is no session to hang an identity on — and that is a better fit for
    this platform than the alternative: an identity established once and reused
    is an identity that outlives the token that proved it.
    """

    def __init__(
        self,
        identity: IdentityProvider,
        retrieve: RetrieveChunks,
        repository: DocumentRepository,
        workflows: Mapping[str, AgentWorkflow] | None = None,
        checkpointer: AgentCheckpointer | None = None,
    ) -> None:
        """Initialise the gateway.

        Args:
            identity: Verifies the bearer token and returns the caller.
            retrieve: Retrieval, shared with the rest of the platform.
            repository: Where whole documents are read from.
            workflows: The agents this deployment offers, by name. Empty when
                agents are not exposed over MCP.
            checkpointer: Where runs are recorded, so a finished run can be read
                back with its steps and what it cost.
        """
        self._identity = identity
        self._retrieve = retrieve
        self._repository = repository
        self._workflows = dict(workflows or {})
        self._checkpointer = checkpointer

    async def caller(self, headers: Mapping[str, str] | None) -> Principal:
        """Return the authenticated caller behind a request.

        Raises:
            AuthenticationError: If the caller could not be established.
        """
        return await self._identity.authenticate(bearer_token(headers))

    def executor_for(self, principal: Principal) -> ToolExecutor:
        """Build an executor scoped to one caller's tenant.

        Per call, and bound to the tenant from the **token** rather than from the
        arguments. Over MCP the client is literally a model, so an executor that
        took its tenant from a tool call would take it from something a prompt
        can talk into changing.
        """
        return ToolExecutor(self._retrieve, self._repository, principal.tenant_id)

    async def run(
        self,
        name: str,
        arguments: Mapping[str, object],
        *,
        call_id: str,
        headers: Mapping[str, str] | None,
    ) -> str:
        """Authenticate, then execute one tool call.

        The span is the audit trail ADR-0023 left as an open item: every tool
        call an external client makes, with who made it and which tool. Recorded
        here rather than in the executor because the executor is agent logic and
        does not import instrumentation (ADR-0025) — and because this is the edge
        where the call comes from *outside*, which is the one worth auditing.

        Arguments are not recorded. They are the caller's data, and a query is
        what somebody asked.

        Raises:
            AuthenticationError: If the caller could not be established.
            UnknownToolError: If no tool has that name.
            ToolArgumentError: If the arguments do not fit the tool.
        """
        principal = await self.caller(headers)
        with (
            measured_tool(name),
            operation_span(
                Operation.EXECUTE_TOOL,
                name,
                attributes={
                    TOOL_NAME: name,
                    TOOL_CALL_ID: call_id,
                    # "function" is what the conventions call a tool the
                    # platform runs itself, rather than one the provider runs
                    # for it.
                    TOOL_TYPE: "function",
                    TENANT: principal.tenant_id,
                },
            ),
        ):
            executor = self.executor_for(principal)
            return await executor.run(
                ToolCall(call_id=call_id, name=name, arguments=dict(arguments))
            )

    async def run_agent(
        self, agent: str, question: str, *, headers: Mapping[str, str] | None
    ) -> AgentRun:
        """Run an agent to completion and return its record.

        Run rather than started-and-polled. These graphs are deterministic and
        step-limited, so a run is bounded before it begins; a client that has to
        poll for a result it will get in seconds is a client given work to do for
        no reason. A workflow whose cost is not bounded should not be exposed
        this way, which is a reason to keep them bounded.

        Raises:
            AuthenticationError: If the caller could not be established.
            UnknownAgentError: If no agent has that name.
            AgentRunError: If the run could not be completed.
        """
        principal = await self.caller(headers)
        workflow = self._workflows.get(agent)
        if workflow is None:
            offered = ", ".join(sorted(self._workflows)) or "none"
            msg = f"no agent named '{agent}'; this deployment offers: {offered}"
            raise UnknownAgentError(msg)

        if self._checkpointer is None:  # pragma: no cover - wired together or not at all
            msg = "agents are exposed but no run store is configured"
            raise AgentRunError(msg)

        thread_id = str(uuid.uuid4())
        async for _ in workflow.stream(
            question, thread_id=thread_id, tenant_id=principal.tenant_id
        ):
            pass

        run = await self._checkpointer.load(thread_id)
        if run is None:  # pragma: no cover - the workflow saves before it yields
            msg = f"run '{thread_id}' finished without being recorded"
            raise AgentRunError(msg)
        return run


GatewayFactory = Callable[[], McpToolGateway]

__all__ = [
    "AuthenticationError",
    "GatewayFactory",
    "McpToolGateway",
    "UnknownAgentError",
    "bearer_token",
]
