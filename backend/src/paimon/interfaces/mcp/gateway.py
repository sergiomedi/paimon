"""Turning an MCP tool call into an authenticated, tenant-scoped execution.

Separate from the server so the decision that matters — who is calling, and
whose material they may read — is a plain class with plain tests, rather than
something only reachable through a protocol session.
"""

from collections.abc import Callable, Mapping

from paimon.agents.tools import ToolExecutor
from paimon.application.use_cases.retrieve_chunks import RetrieveChunks
from paimon.domain.entities import Principal
from paimon.domain.errors import AuthenticationError, InvalidTokenError
from paimon.domain.ports import DocumentRepository, IdentityProvider, ToolCall

BEARER = "bearer"


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
    ) -> None:
        """Initialise the gateway.

        Args:
            identity: Verifies the bearer token and returns the caller.
            retrieve: Retrieval, shared with the rest of the platform.
            repository: Where whole documents are read from.
        """
        self._identity = identity
        self._retrieve = retrieve
        self._repository = repository

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

        Raises:
            AuthenticationError: If the caller could not be established.
            UnknownToolError: If no tool has that name.
            ToolArgumentError: If the arguments do not fit the tool.
        """
        principal = await self.caller(headers)
        executor = self.executor_for(principal)
        return await executor.run(ToolCall(call_id=call_id, name=name, arguments=dict(arguments)))


GatewayFactory = Callable[[], McpToolGateway]

__all__ = ["AuthenticationError", "GatewayFactory", "McpToolGateway", "bearer_token"]
