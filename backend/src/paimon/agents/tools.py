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
from dataclasses import dataclass
from typing import Any

from paimon.application.use_cases.retrieve_chunks import RetrieveChunks
from paimon.domain.entities import Chunk
from paimon.domain.errors import DomainError
from paimon.domain.ports import (
    DocumentRepository,
    SearchFilters,
    ToolCall,
    ToolDefinition,
    VectorStore,
)

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

#: Most chunks a document read will fetch before the character budget is applied.
#: A second bound, on the store rather than on the text, so a pathological
#: document costs a bounded query rather than a bounded answer to an unbounded one.
MAX_DOCUMENT_CHUNKS = 60

#: Said plainly, because a model given an empty result and no explanation tends
#: to fill the silence from its own memory.
NOTHING_MATCHED = "No indexed passages matched that query. Do not answer from memory."

#: Truncation is announced. A model that cannot tell it received part of a
#: procedure will describe the part it got as the whole of it.
TRUNCATION_NOTE = "[truncated: document continues beyond this point]"


class UnknownToolError(DomainError):
    """A model asked for a tool this platform does not offer."""


class ToolArgumentError(DomainError):
    """A model asked for a tool with arguments it cannot be run on."""


@dataclass(frozen=True, slots=True)
class ToolResult:
    """What a tool found, before anybody decided how to number it.

    Separating the passages from their rendering is what lets one execution path
    serve two readers that need different numbering. An MCP client gets a result
    numbered from one, because each of its calls stands alone. An agent in a loop
    needs a number that means the same thing on turn three as it did on turn one,
    because its final answer cites across the whole run — and only the caller
    knows what it has already seen.

    Attributes:
        passages: What may be cited, in the order it should be read. Chunks
            rather than text, so every passage carries the document and the
            offsets a citation resolves against.
        note: What to tell the model besides the passages — that nothing
            matched, that the document does not exist, that there is more.
            Rendered after them, never instead of them.
        truncated: Whether material was withheld for budget.
    """

    passages: tuple[Chunk, ...] = ()
    note: str = ""
    truncated: bool = False


def render_passages(passages: Sequence[Chunk], markers: Sequence[int]) -> str:
    """Render numbered passages for a model to read and cite.

    Args:
        passages: What to show.
        markers: The number each passage is to carry, in the same order. Passed
            in rather than derived, because the whole difference between a
            single call and a running conversation is who decides the numbering.

    Returns:
        The rendered block.

    Raises:
        ValueError: If there is not exactly one marker per passage. A passage
            rendered under the wrong number is a citation that resolves to the
            wrong text, which is worse than one that does not resolve at all.
    """
    if len(passages) != len(markers):
        msg = f"{len(passages)} passages and {len(markers)} markers"
        raise ValueError(msg)
    return "\n\n".join(
        f"[{marker}] document: {passage.document_id}\n{passage.text}"
        for passage, marker in zip(passages, markers, strict=True)
    )


class ToolExecutor:
    """Runs the tools a model asks for, against this tenant's material.

    The tenant is bound at construction and never read from a tool call. A model
    that could name the tenant it wanted to search would be a model that could
    ask for another organization's runbooks, and no prompt is a security boundary.
    """

    def __init__(
        self,
        retrieve: RetrieveChunks,
        repository: DocumentRepository,
        store: VectorStore,
        tenant_id: str,
    ) -> None:
        """Initialise the executor.

        Args:
            retrieve: Retrieval, already configured.
            repository: Where whole documents are read from, for a client that
                wants the document rather than passages out of it.
            store: Where a document's indexed chunks are read from, for a caller
                that has to cite what it read.
            tenant_id: The isolation boundary for every call.
        """
        self._retrieve = retrieve
        self._repository = repository
        self._store = store
        self._tenant_id = tenant_id

    async def execute(self, call: ToolCall) -> ToolResult:
        """Run one tool call and return what it found, unnumbered.

        The path an agent uses. Both tools come back as passages here, including
        ``read_document``: a run that cites what it read needs offsets, and a
        document returned as one long string can only be cited as the whole of
        itself.

        Returns:
            The passages and whatever has to be said about them.

        Raises:
            UnknownToolError: If no tool has that name.
            ToolArgumentError: If the arguments do not fit the tool.
        """
        if call.name == SEARCH_CORPUS.name:
            return await self._search_passages(call.arguments)
        if call.name == READ_DOCUMENT.name:
            return await self._read_passages(call.arguments)
        raise UnknownToolError(self._unknown(call.name))

    async def run(self, call: ToolCall) -> str:
        """Execute one tool call and render its result for the model.

        The path an MCP client uses, where every call stands alone and is
        therefore numbered from one. ``read_document`` returns the document's
        text rather than its chunks, deliberately: chunking overlaps, so a
        document reassembled from its passages would repeat itself, and an
        external client asking for a document is asking for the document.

        Returns:
            The result as text, ready to be sent back in a ``tool`` message.

        Raises:
            UnknownToolError: If no tool has that name.
            ToolArgumentError: If the arguments do not fit the tool.
        """
        if call.name == SEARCH_CORPUS.name:
            found = await self._search_passages(call.arguments)
            if not found.passages:
                return found.note
            return render_passages(found.passages, range(1, len(found.passages) + 1))
        if call.name == READ_DOCUMENT.name:
            return await self._read_text(call.arguments)
        raise UnknownToolError(self._unknown(call.name))

    @staticmethod
    def _unknown(name: str) -> str:
        """Say which tools exist, because a model can act on that."""
        offered = ", ".join(tool.name for tool in TOOLS)
        return f"no tool named '{name}'; this platform offers: {offered}"

    async def _search_passages(self, arguments: Mapping[str, Any]) -> ToolResult:
        query = str(arguments.get("query") or "").strip()
        if not query:
            msg = "search_corpus needs a non-empty query"
            raise ToolArgumentError(msg)
        limit = _bounded(arguments.get("limit", 5), low=1, high=20, name="limit")

        result = await self._retrieve(query, SearchFilters(tenant_id=self._tenant_id))
        hits = list(result.hits)[:limit]
        if not hits:
            return ToolResult(note=NOTHING_MATCHED)
        return ToolResult(passages=tuple(hit.chunk for hit in hits))

    async def _read_passages(self, arguments: Mapping[str, Any]) -> ToolResult:
        document_id = self._document_id(arguments)
        chunks = await self._store.list_chunks(
            self._tenant_id, document_id, limit=MAX_DOCUMENT_CHUNKS
        )
        if not chunks:
            return ToolResult(note=self._missing(document_id))

        kept: list[Chunk] = []
        spent = 0
        for chunk in chunks:
            if kept and spent + len(chunk.text) > MAX_DOCUMENT_CHARACTERS:
                break
            kept.append(chunk)
            spent += len(chunk.text)
        truncated = len(kept) < len(chunks)
        return ToolResult(
            passages=tuple(kept),
            note=TRUNCATION_NOTE if truncated else "",
            truncated=truncated,
        )

    async def _read_text(self, arguments: Mapping[str, Any]) -> str:
        document_id = self._document_id(arguments)
        document = await self._repository.get(self._tenant_id, document_id)
        if document is None:
            return self._missing(document_id)
        text = document.text
        if len(text) > MAX_DOCUMENT_CHARACTERS:
            return f"{text[:MAX_DOCUMENT_CHARACTERS]}\n\n{TRUNCATION_NOTE}"
        return text

    @staticmethod
    def _document_id(arguments: Mapping[str, Any]) -> str:
        document_id = str(arguments.get("document_id") or "").strip()
        if not document_id:
            msg = "read_document needs a document_id"
            raise ToolArgumentError(msg)
        return document_id

    @staticmethod
    def _missing(document_id: str) -> str:
        return f"No document '{document_id}' is indexed for this tenant."


@dataclass(frozen=True, slots=True)
class CorpusAccess:
    """Everything the two tools run against, before a tenant is named.

    The linter found this one, the same way it found
    :class:`~paimon.agents.collaborators.AgentCollaborators`: a constructor that
    took three retrieval collaborators in a row plus two more tripped the
    argument-count rule, which is what that rule is for.

    Naming it is worth more than the line it saves. An executor is **per
    tenant** and the tenant comes from an authenticated caller or from a run's
    state, never from a tool call — so what a deployment actually holds is this,
    a corpus nobody can read yet, and binding it to a tenant is a deliberate act
    with a name.
    """

    retrieve: RetrieveChunks
    repository: DocumentRepository
    store: VectorStore

    def for_tenant(self, tenant_id: str) -> "ToolExecutor":
        """Bind this corpus to one tenant's material.

        Args:
            tenant_id: The isolation boundary. From a verified token or from the
                run being executed — a prompt is not a security boundary.
        """
        return ToolExecutor(self.retrieve, self.repository, self.store, tenant_id)


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
