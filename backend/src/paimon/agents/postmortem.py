"""Postmortem drafting, with incident triage reused as a component.

The composition Uber's deployment demonstrated: rather than reimplement "find
what is known about this incident", this agent embeds the triage agent whole and
then writes over its conclusion. Splicing happens on the description, so the
orchestrator runs one flat graph and every triage node still appears in the trace
under ``precedent.*`` — which a nested runtime would have hidden.

Two consequences of the embedded agent sharing this one's state are load-bearing
here, and both were found by wiring it up rather than by reasoning about it.

The timeline is injected as evidence **after** triage, not before. Injecting it
first would have left triage's own "is there anything to work from" branch always
answering yes, so the sub-agent would have called a model on a corpus that had
returned nothing — the parent's write silently changing the sub-agent's routing.

And triage's conclusion is copied into ``notes`` before this agent overwrites
``draft``. A field two parts of a run share is a field the second part has to
read before it writes.
"""

from paimon.agents.support import load_documents
from paimon.agents.triage import build_triage_graph
from paimon.application.use_cases.retrieve_chunks import RetrieveChunks
from paimon.domain.agents import (
    END,
    AgentState,
    GraphSpec,
    NodeSpec,
    StateUpdate,
    StepReport,
    embed,
)
from paimon.domain.entities import Chunk
from paimon.domain.ports import ChatModel, DocumentRepository, TokenCounter
from paimon.rag.citations import resolve_citations
from paimon.rag.prompting import DEFAULT_CONTEXT_TOKENS, build_prompt

AGENT_NAME = "postmortem-drafting"

#: The document id the incident's own timeline is cited under. It is not in the
#: corpus and never will be, so it has an id of its own rather than borrowing one.
TIMELINE_DOCUMENT_ID = "incident-timeline"

SECTIONS = ("Summary", "Impact", "Timeline", "Root cause", "What went well", "Action items")

INSTRUCTION = (
    "Draft a postmortem with these sections: {sections}. "
    "Write only what the sources support, and cite each claim.\n\n"
    "Incident timeline:\n{timeline}"
)
PRECEDENT = "\n\nWhat comparable earlier incidents suggest:\n{precedent}"

UNSUPPORTED = (
    "I could not tie a postmortem draft to the timeline or to any earlier incident, "
    "so I am not offering one."
)


def timeline_chunk(timeline: str, tenant_id: str) -> Chunk:
    """Present the incident's timeline as a citable source.

    A postmortem's primary source is the incident itself. Without this the
    timeline would be context the model was told to trust but could not cite, and
    every claim resting on it would be withdrawn as unsupported.
    """
    return Chunk(
        chunk_id=f"{TIMELINE_DOCUMENT_ID}:0",
        document_id=TIMELINE_DOCUMENT_ID,
        tenant_id=tenant_id,
        ordinal=0,
        text=timeline,
        start_char=0,
        end_char=len(timeline),
        token_count=max(len(timeline.split()), 1),
    )


def build_postmortem_graph(
    retrieve: RetrieveChunks,
    chat_model: ChatModel,
    repository: DocumentRepository,
    token_counter: TokenCounter,
    *,
    max_context_tokens: int = DEFAULT_CONTEXT_TOKENS,
) -> GraphSpec:
    """Assemble the postmortem agent, with triage embedded as its first phase.

    Args:
        retrieve: Retrieval, shared with the embedded triage agent.
        chat_model: Called once by triage and once here.
        repository: Loads the documents behind cited chunks.
        token_counter: Enforces the context budget.
        max_context_tokens: Budget for the sources section of the prompt.

    Returns:
        A validated graph specification.
    """
    triage = embed(
        build_triage_graph(
            retrieve,
            chat_model,
            repository,
            token_counter,
            max_context_tokens=max_context_tokens,
        ),
        "precedent",
        exit_to="ground",
    )

    async def read(state: AgentState) -> StateUpdate:
        """Normalise the timeline. Nothing else happens before triage runs."""
        return {"question": state.question.strip()}

    async def ground(state: AgentState) -> StateUpdate:
        """Keep what triage concluded, and make the timeline citable."""
        return {
            "notes": state.draft,
            "evidence": (timeline_chunk(state.question, state.tenant_id),),
        }

    async def compose(state: AgentState) -> StateUpdate:
        """Draft the postmortem over the timeline and the precedent."""
        instruction = INSTRUCTION.format(sections=", ".join(SECTIONS), timeline=state.question)
        if state.notes:
            instruction += PRECEDENT.format(precedent=state.notes)

        prompt = build_prompt(instruction, state.evidence, token_counter, max_context_tokens)
        completion = await chat_model.complete(list(prompt.messages))
        documents = await load_documents(repository, prompt.sources, state.tenant_id)
        cited = resolve_citations(completion.text, prompt.sources, documents)
        return {"draft": cited.text, "citations": cited.citations}

    async def verify(state: AgentState) -> StateUpdate:
        """Withdraw a draft that cites nothing, by the same rule as triage."""
        if state.citations:
            return {}
        return {"draft": UNSUPPORTED, "citations": ()}

    return GraphSpec(
        name=AGENT_NAME,
        entry="read",
        nodes=[
            NodeSpec(name="read", run=read, summary="read the incident timeline"),
            *triage.nodes,
            NodeSpec(
                name="ground",
                run=ground,
                summary="kept the precedent and made the timeline citable",
                report=_report_precedent,
            ),
            NodeSpec(name="compose", run=compose, summary="drafted the postmortem"),
            NodeSpec(name="verify", run=verify, summary="checked the draft is supported"),
        ],
        edges=[
            ("read", triage.entry),
            *triage.edges,
            ("ground", "compose"),
            ("compose", "verify"),
            ("verify", END),
        ],
        branches=list(triage.branches),
    )


def _report_precedent(state: AgentState, _update: StateUpdate) -> StepReport:
    """Say how much earlier material the embedded agent found."""
    sources = len({chunk.document_id for chunk in state.evidence})
    return StepReport(
        summary=f"carried precedent from {sources} earlier documents",
        details={"precedent_documents": str(sources)},
    )
