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

from paimon.agents import AGENTS
from paimon.agents.tools import READ_DOCUMENT, SEARCH_CORPUS
from paimon.domain.entities import AgentRun
from paimon.domain.errors import DomainError
from paimon.interfaces.mcp.gateway import GatewayFactory

SERVER_NAME = "paimon"

RUN_AGENT = "run_agent"

#: Documents are **not** offered as MCP resources, and the reason is a limitation
#: rather than a preference. A resource template's function is wrapped in
#: pydantic's ``validate_call``, which revalidates its arguments — and a
#: revalidated ``Context`` is a copy that has lost its binding to the request.
#: Reading ``context.headers`` inside a template therefore raises "Context is not
#: available outside of a request" even in the middle of one.
#:
#: No request means no bearer token, and no token means no tenant. A resource
#: that served documents without establishing whose they are is not a feature
#: worth having, so ``read_document`` remains a tool — where the context does
#: survive — until the SDK exposes request state to templates.

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

    @server.tool(
        name=RUN_AGENT,
        description=(
            "Run one of this platform's operational agents to completion and return "
            "its answer with the steps it took. Agents search the corpus themselves; "
            "use this instead of search_corpus when the task is triaging an incident, "
            "drafting a postmortem from a timeline, or assessing what a topic's "
            "documentation leaves out."
        ),
    )
    async def run_agent(
        context: Context[Any, Any],
        agent: Annotated[
            str,
            Field(description=f"Which agent to run. One of: {', '.join(sorted(AGENTS))}."),
        ],
        input: Annotated[  # noqa: A002  the protocol's own word for it
            str,
            Field(description="The symptom, incident timeline or topic, per the agent."),
        ],
    ) -> str:
        """Run an agent and render what it produced."""
        try:
            run = await gateway().run_agent(agent, input, headers=context.headers)
        except DomainError as error:
            return f"error: {error}"
        return render_run(run)

    return server


def render_run(run: AgentRun) -> str:
    """Render a finished run for a model to read.

    The steps are included, not just the answer. A model deciding whether to
    trust an answer benefits from seeing that retrieval found four passages
    across two documents — and from seeing when it found none.
    """
    steps = "\n".join(f"  - {step.name}: {step.summary}" for step in run.steps)
    return (
        f"agent: {run.agent}\n"
        f"run: {run.thread_id}\n"
        f"status: {run.status}\n"
        f"tokens: {run.total_tokens}\n"
        f"steps:\n{steps}\n\n"
        f"{run.answer or '(the run produced no text)'}"
    )
