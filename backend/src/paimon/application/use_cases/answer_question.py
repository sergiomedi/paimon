"""Answering a question from indexed material."""

from collections.abc import Sequence
from dataclasses import dataclass

from paimon.application.use_cases.retrieve_chunks import RetrieveChunks, Strategy
from paimon.domain.entities import Chunk, Document
from paimon.domain.ports import ChatModel, DocumentRepository, SearchFilters, TokenCounter
from paimon.domain.value_objects import Citation
from paimon.rag.citations import resolve_citations
from paimon.rag.prompting import DEFAULT_CONTEXT_TOKENS, build_prompt

NO_MATERIAL = "I have no indexed material that bears on that question, so I cannot answer it."


@dataclass(frozen=True, slots=True)
class Usage:
    """What answering the question cost."""

    model_id: str
    input_tokens: int
    output_tokens: int

    @property
    def total_tokens(self) -> int:
        """Tokens consumed by the request and its answer."""
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True, slots=True)
class Answer:
    """A grounded answer, or an honest refusal to give one.

    Attributes:
        text: The answer.
        citations: Sources the answer actually referred to.
        grounded: Whether the answer cites anything at all.
        strategy: How retrieval reached its candidates.
        retrieved: How many chunks retrieval returned.
        used_sources: How many of them fitted in the prompt.
        dropped_markers: Markers the model used that referred to no source.
        usage: What the generation cost, when a model was called.
    """

    text: str
    citations: tuple[Citation, ...]
    grounded: bool
    strategy: Strategy
    retrieved: int
    used_sources: int
    dropped_markers: tuple[int, ...] = ()
    usage: Usage | None = None


class AnswerQuestion:
    """Retrieves, then answers only from what was retrieved.

    Two behaviours make this different from a chat wrapper.

    When retrieval finds nothing, no model is called. The platform says it has
    nothing rather than inviting a fluent answer from parametric memory, which is
    the failure mode that makes such systems untrustworthy: an answer that sounds
    right and is not in the sources is worse than no answer, because the reader
    cannot tell the difference.

    Every claim the model makes is expected to carry a marker, and markers that
    point outside the source list are removed and counted. A model that invents
    references is a fact worth monitoring, not one to hide.
    """

    def __init__(
        self,
        retrieve: RetrieveChunks,
        chat_model: ChatModel,
        repository: DocumentRepository,
        token_counter: TokenCounter,
        max_context_tokens: int = DEFAULT_CONTEXT_TOKENS,
    ) -> None:
        """Initialise the use case with the collaborators it needs."""
        self._retrieve = retrieve
        self._chat_model = chat_model
        self._repository = repository
        self._token_counter = token_counter
        self._max_context_tokens = max_context_tokens

    async def __call__(self, question: str, filters: SearchFilters) -> Answer:
        """Answer a question from the indexed corpus.

        Args:
            question: The question, as asked.
            filters: Tenant and any further restrictions.

        Returns:
            The answer and the sources it rests on, or a refusal when nothing
            relevant was found.
        """
        retrieval = await self._retrieve(question, filters)
        if not retrieval.hits:
            return Answer(
                text=NO_MATERIAL,
                citations=(),
                grounded=False,
                strategy=retrieval.strategy,
                retrieved=0,
                used_sources=0,
            )

        chunks = [hit.chunk for hit in retrieval.hits]
        prompt = build_prompt(question, chunks, self._token_counter, self._max_context_tokens)
        completion = await self._chat_model.complete(list(prompt.messages))

        documents = await self._load_documents(prompt.sources, filters.tenant_id)
        cited = resolve_citations(completion.text, prompt.sources, documents)

        return Answer(
            text=cited.text,
            citations=cited.citations,
            grounded=cited.is_grounded,
            strategy=retrieval.strategy,
            retrieved=len(retrieval.hits),
            used_sources=len(prompt.sources),
            dropped_markers=cited.dropped_markers,
            usage=Usage(
                model_id=completion.model_id,
                input_tokens=completion.input_tokens,
                output_tokens=completion.output_tokens,
            ),
        )

    async def _load_documents(
        self, sources: Sequence[Chunk], tenant_id: str
    ) -> dict[str, Document]:
        """Load the documents behind the cited chunks, once each."""
        document_ids = {chunk.document_id for chunk in sources}
        documents: dict[str, Document] = {}
        for document_id in sorted(document_ids):
            document = await self._repository.get(tenant_id, document_id)
            if document is not None:
                documents[document_id] = document
        return documents
