"""Paimon as an MCP server.

An MCP server is an interface, exactly like the HTTP API: same layer, same
composition root, same authentication. It exposes the tools declared in
:mod:`paimon.agents.tools` — the same declarations a model provider is offered
and the same ones the executor runs — so the platform has one definition of what
a tool is and does, rather than one per consumer.

The 2026-07-28 specification made the protocol stateless: no ``initialize``
handshake, no session id, every request carrying its own context. That suits a
service with a tenant to establish, because there is no session for an identity
to outlive.
"""

from typing import Annotated, Any

from mcp.server.mcpserver import Context, MCPServer
from pydantic import Field

from paimon.agents.tools import READ_DOCUMENT, SEARCH_CORPUS
from paimon.domain.errors import DomainError
from paimon.interfaces.mcp.gateway import GatewayFactory

SERVER_NAME = "paimon"

INSTRUCTIONS = """Paimon indexes an organization's operational knowledge — runbooks,
postmortems, architecture decisions and API references — and answers from it with
citations that resolve to an exact span of a source document.

Search before answering any question about how this organization's systems behave or
what was done previously. When a search returns nothing, say so; do not answer from
memory, because the whole value of this corpus is that its answers can be checked."""


def build_mcp_server(gateway: GatewayFactory) -> MCPServer:
    """Assemble the MCP server.

    Args:
        gateway: Produces the authenticating, tenant-scoping gateway. A factory
            rather than an instance because the server is built once at startup
            while the gateway depends on process-lifetime resources that the
            composition root owns.

    Returns:
        A server ready to be mounted as an ASGI application.
    """
    server = MCPServer(name=SERVER_NAME, instructions=INSTRUCTIONS)

    async def _run(context: Context[Any, Any], name: str, arguments: dict[str, Any]) -> str:
        try:
            return await gateway().run(
                name, arguments, call_id=context.request_id, headers=context.headers
            )
        except DomainError as error:
            # Surfaced as the message rather than as a traceback: the client is a
            # model, and a model reads this. "No document 'x' is indexed" is
            # something it can act on; a stack trace is something it will
            # paraphrase into a claim about the corpus.
            return f"error: {error}"

    @server.tool(name=SEARCH_CORPUS.name, description=SEARCH_CORPUS.description)
    async def search_corpus(
        context: Context[Any, Any],
        query: Annotated[
            str,
            Field(description="What to look for, phrased as the question being asked."),
        ],
        limit: Annotated[int, Field(description="Maximum passages to return.", ge=1, le=20)] = 5,
    ) -> str:
        """Search the indexed operational corpus."""
        return await _run(context, SEARCH_CORPUS.name, {"query": query, "limit": limit})

    @server.tool(name=READ_DOCUMENT.name, description=READ_DOCUMENT.description)
    async def read_document(
        context: Context[Any, Any],
        document_id: Annotated[str, Field(description="Identifier as returned by search_corpus.")],
    ) -> str:
        """Read a whole document by its identifier."""
        return await _run(context, READ_DOCUMENT.name, {"document_id": document_id})

    return server
