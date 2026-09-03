"""Incident triage: what to do about a symptom, and whether it has happened before.

The first agent, and the one that tests whether ADR-0016 was right. Its graph is
fixed: frame the symptom, retrieve twice in parallel, assess what came back,
either refuse or draft, then verify. A model is called at exactly one node. Every
other decision — how to frame, whether there is enough material, whether the
draft is actually supported — is ordinary code, which is what makes a run
reproducible and therefore scoreable in Phase 6.

Two retrievals rather than one because a symptom is two questions. "The eviction
hangs" asks *what do I do*, which lives in runbooks, and *has this happened
before*, which lives in postmortems. One embedding of the raw symptom sits
between the two and retrieves neither well. The branches run concurrently and
their results are merged by the ``evidence`` reducer, which is where
deduplication earns its place: both framings routinely find the same chunk.
"""

from collections.abc import Callable, Sequence

from paimon.application.use_cases.answer_question import NO_MATERIAL
from paimon.application.use_cases.retrieve_chunks import RetrieveChunks
from paimon.domain.agents import (
    END,
    AgentState,
    Branch,
    GraphSpec,
    NodeSpec,
    StateUpdate,
    StepReport,
)
from paimon.domain.entities import Chunk, Document
from paimon.domain.ports import ChatModel, DocumentRepository, SearchFilters, TokenCounter
from paimon.rag.citations import resolve_citations
from paimon.rag.prompting import DEFAULT_CONTEXT_TOKENS, build_prompt

AGENT_NAME = "incident-triage"

#: How the symptom is put to each retriever. Templates rather than a model call:
#: a rewriting model makes the same run retrieve different material on different
#: days, and ADR-0016 spends model calls on content, not on control flow.
PROCEDURE_FRAMING = "What is the procedure to resolve: {symptom}"
HISTORY_FRAMING = "Has this happened before, and what was the cause: {symptom}"

UNSUPPORTED = (
    "I found material on this incident but could not tie the draft to any of it, "
    "so I am not offering it as an answer."
)


def frame_symptom(symptom: str) -> tuple[str, str]:
    """Return the procedure framing and the history framing of a symptom."""
    cleaned = " ".join(symptom.split())
    return PROCEDURE_FRAMING.format(symptom=cleaned), HISTORY_FRAMING.format(symptom=cleaned)


def build_triage_graph(
    retrieve: RetrieveChunks,
    chat_model: ChatModel,
    repository: DocumentRepository,
    token_counter: TokenCounter,
    *,
    max_context_tokens: int = DEFAULT_CONTEXT_TOKENS,
) -> GraphSpec:
    """Assemble the triage agent from the use cases it orchestrates.

    A factory rather than a class: what a node needs is captured in a closure at
    build time, so a node stays a function of the state alone and can be called
    in a test without constructing an agent.

    Args:
        retrieve: Retrieval, already configured with a store and an embedding model.
        chat_model: Used at one node, to draft.
        repository: Loads the documents behind cited chunks, so a citation can
            resolve to a span of the source rather than to a chunk id.
        token_counter: Enforces the context budget.
        max_context_tokens: Budget for the sources section of the prompt.

    Returns:
        A validated graph specification.
    """

    async def frame(state: AgentState) -> StateUpdate:
        """Record the symptom under investigation. No model, no retrieval."""
        return {"question": " ".join(state.question.split())}

    async def _retrieve_with(state: AgentState, framing: str) -> tuple[Chunk, ...]:
        result = await retrieve(framing, SearchFilters(tenant_id=state.tenant_id))
        return tuple(hit.chunk for hit in result.hits)

    async def procedure(state: AgentState) -> StateUpdate:
        """Look for the runbook: what to do about this."""
        procedure_query, _ = frame_symptom(state.question)
        return {"evidence": await _retrieve_with(state, procedure_query)}

    async def history(state: AgentState) -> StateUpdate:
        """Look for the postmortem: whether this has happened before."""
        _, history_query = frame_symptom(state.question)
        return {"evidence": await _retrieve_with(state, history_query)}

    async def assess(_state: AgentState) -> StateUpdate:
        """The merge point of the two retrievals, and where routing is decided.

        Writes nothing. It exists because two parallel branches need somewhere to
        join before a branch can be taken on their combined result, and because
        the run's trace should show that the joining happened. Its step report
        reads the merged state, which is the whole of what it has to say.
        """
        return {}

    async def refuse(_state: AgentState) -> StateUpdate:
        """Say there is nothing, without calling a model.

        The conviction the whole platform is built on, expressed at graph level:
        when retrieval is empty there is nothing to be grounded in, and a fluent
        answer from parametric memory is worse than no answer because the reader
        cannot tell the difference.
        """
        return {"draft": NO_MATERIAL, "citations": ()}

    async def draft(state: AgentState) -> StateUpdate:
        """Draft an answer from the merged evidence. The one model call."""
        prompt = build_prompt(state.question, state.evidence, token_counter, max_context_tokens)
        completion = await chat_model.complete(list(prompt.messages))
        documents = await _load_documents(repository, prompt.sources, state.tenant_id)
        cited = resolve_citations(completion.text, prompt.sources, documents)
        return {"draft": cited.text, "citations": cited.citations}

    async def verify(state: AgentState) -> StateUpdate:
        """Refuse a draft that cites nothing, after the model has spoken.

        Deterministic on purpose. A model asked to check its own grounding will
        usually agree with itself; whether a marker resolved to a real span is a
        fact the platform already knows, and asking is both slower and less
        reliable than looking.
        """
        if state.citations:
            return {}
        return {"draft": UNSUPPORTED, "citations": ()}

    return GraphSpec(
        name=AGENT_NAME,
        entry="frame",
        nodes=[
            NodeSpec(name="frame", run=frame, summary="framed the symptom"),
            NodeSpec(
                name="procedure",
                run=procedure,
                summary="searched for a procedure",
                report=_report_evidence("procedure"),
            ),
            NodeSpec(
                name="history",
                run=history,
                summary="searched for precedent",
                report=_report_evidence("history"),
            ),
            NodeSpec(
                name="assess",
                run=assess,
                summary="weighed the evidence",
                report=_report_assessment,
            ),
            NodeSpec(name="refuse", run=refuse, summary="found nothing to answer from"),
            NodeSpec(name="draft", run=draft, summary="drafted a grounded answer"),
            NodeSpec(name="verify", run=verify, summary="checked the draft is supported"),
        ],
        edges=[
            ("frame", "procedure"),
            ("frame", "history"),
            ("procedure", "assess"),
            ("history", "assess"),
            ("refuse", END),
            ("draft", "verify"),
            ("verify", END),
        ],
        branches=[
            Branch(
                source="assess",
                decide=lambda state: "draft" if state.evidence else "refuse",
                targets={"draft": "draft", "refuse": "refuse"},
            )
        ],
    )


def _report_evidence(retriever: str) -> Callable[[AgentState, StateUpdate], StepReport]:
    """Build the step reporter for a retrieval node."""

    def report(_state: AgentState, update: StateUpdate) -> StepReport:
        found = len(update.get("evidence", ()))
        return StepReport(
            summary=f"{retriever}: retrieved {found} chunk{'' if found == 1 else 's'}",
            details={"retriever": retriever, "chunks": str(found)},
        )

    return report


def _report_assessment(state: AgentState, _update: StateUpdate) -> StepReport:
    """Describe the merged evidence, which is what this node exists to see."""
    found = len(state.evidence)
    documents = len({chunk.document_id for chunk in state.evidence})
    return StepReport(
        summary=f"weighed {found} chunks from {documents} documents",
        details={"chunks": str(found), "documents": str(documents)},
    )


async def _load_documents(
    repository: DocumentRepository, sources: Sequence[Chunk], tenant_id: str
) -> dict[str, Document]:
    """Load each cited chunk's document once."""
    documents: dict[str, Document] = {}
    for document_id in sorted({chunk.document_id for chunk in sources}):
        document = await repository.get(tenant_id, document_id)
        if document is not None:
            documents[document_id] = document
    return documents
