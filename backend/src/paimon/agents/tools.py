"""What the platform lets a model ask it to do.

Two tools, and the deliberate smallness is the design. Anthropic's guidance is
that tool design deserves the engineering a prompt gets, and that a tool should
be hard to misuse; a wide surface invites a model to pick the wrong one, and
every tool added is a tool every call has to be given a reason not to choose.

These declarations are also what Phase 4 exposes over MCP. One definition, read
by a model provider, by an MCP client and by the executor below — rather than
three that drift.
"""

from collections.abc import Mapping, Sequence
from typing import Any

from paimon.application.use_cases.retrieve_chunks import RetrieveChunks
from paimon.domain.errors import DomainError
from paimon.domain.ports import DocumentRepository, SearchFilters, ToolCall, ToolDefinition

SEARCH_CORPUS = ToolDefinition(
    name="search_corpus",
    description=(
        "Search the indexed operational corpus — runbooks, postmortems, ADRs and "
        "API references — for passages relevant to a query. Returns passages with "
        "the document they came from. Use this before answering any question "
        "about how the system behaves or what was done previously."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What to look for, phrased as the question being asked.",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum passages to return.",
                "minimum": 1,
                "maximum": 20,
                "default": 5,
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    },
)

READ_DOCUMENT = ToolDefinition(
    name="read_document",
    description=(
        "Read a whole document by its identifier, when a retrieved passage is not "
        "enough context. Identifiers come from search_corpus results; this tool "
        "cannot find a document by title."
    ),
    parameters={
        "type": "object",
        "properties": {
            "document_id": {
                "type": "string",
                "description": "Identifier as returned by search_corpus.",
            }
        },
        "required": ["document_id"],
        "additionalProperties": False,
    },
)

TOOLS: tuple[ToolDefinition, ...] = (SEARCH_CORPUS, READ_DOCUMENT)

#: Documents are long, and a tool result that fills the context window leaves no
#: room for the reasoning it was fetched for.
MAX_DOCUMENT_CHARACTERS = 8000


class UnknownToolError(DomainError):
    """A model asked for a tool this platform does not offer."""


class ToolArgumentError(DomainError):
    """A model asked for a tool with arguments it cannot be run on."""


class ToolExecutor:
    """Runs the tools a model asks for, against this tenant's material.

    The tenant is bound at construction and never read from a tool call. A model
    that could name the tenant it wanted to search would be a model that could
    ask for another organization's runbooks, and no prompt is a security boundary.
    """

    def __init__(
        self, retrieve: RetrieveChunks, repository: DocumentRepository, tenant_id: str
    ) -> None:
        """Initialise the executor.

        Args:
            retrieve: Retrieval, already configured.
            repository: Where whole documents are read from.
            tenant_id: The isolation boundary for every call.
        """
        self._retrieve = retrieve
        self._repository = repository
        self._tenant_id = tenant_id

    async def run(self, call: ToolCall) -> str:
        """Execute one tool call and render its result for the model.

        Returns:
            The result as text, ready to be sent back in a ``tool`` message.

        Raises:
            UnknownToolError: If no tool has that name.
            ToolArgumentError: If the arguments do not fit the tool.
        """
        if call.name == SEARCH_CORPUS.name:
            return await self._search(call.arguments)
        if call.name == READ_DOCUMENT.name:
            return await self._read(call.arguments)
        offered = ", ".join(tool.name for tool in TOOLS)
        msg = f"no tool named '{call.name}'; this platform offers: {offered}"
        raise UnknownToolError(msg)

    async def _search(self, arguments: Mapping[str, Any]) -> str:
        query = str(arguments.get("query") or "").strip()
        if not query:
            msg = "search_corpus needs a non-empty query"
            raise ToolArgumentError(msg)
        limit = _bounded(arguments.get("limit", 5), low=1, high=20, name="limit")

        result = await self._retrieve(query, SearchFilters(tenant_id=self._tenant_id))
        hits = list(result.hits)[:limit]
        if not hits:
            # Said plainly, because a model given an empty result and no
            # explanation tends to fill the silence from its own memory.
            return "No indexed passages matched that query. Do not answer from memory."
        return "\n\n".join(
            f"[{index}] document: {hit.chunk.document_id}\n{hit.chunk.text}"
            for index, hit in enumerate(hits, start=1)
        )

    async def _read(self, arguments: Mapping[str, Any]) -> str:
        document_id = str(arguments.get("document_id") or "").strip()
        if not document_id:
            msg = "read_document needs a document_id"
            raise ToolArgumentError(msg)

        document = await self._repository.get(self._tenant_id, document_id)
        if document is None:
            return f"No document '{document_id}' is indexed for this tenant."
        text = document.text
        if len(text) > MAX_DOCUMENT_CHARACTERS:
            kept = text[:MAX_DOCUMENT_CHARACTERS]
            # Truncation is announced. A model that cannot tell it received part
            # of a procedure will describe the part it got as the whole of it.
            return f"{kept}\n\n[truncated: document continues beyond this point]"
        return text


def _bounded(value: Any, *, low: int, high: int, name: str) -> int:
    """Read an integer argument, refusing what cannot be one.

    Clamped rather than rejected once it is a number: a model asking for fifty
    passages has misjudged, not malfunctioned, and the platform's own limit is
    the answer. A value that is not a number at all is a different thing.
    """
    try:
        number = int(value)
    except (TypeError, ValueError) as error:
        msg = f"{name} must be a whole number"
        raise ToolArgumentError(msg) from error
    return max(low, min(high, number))


def tool_names(tools: Sequence[ToolDefinition] = TOOLS) -> tuple[str, ...]:
    """The names of a tool set, for logging and for tests."""
    return tuple(tool.name for tool in tools)
