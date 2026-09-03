"""Documentation gap analysis: what a topic's material does not say.

The third agent, and the one whose value is negative space. Retrieval answers
"what do we have on this"; this asks the harder operational question, which is
what an on-call engineer will look for at three in the morning and not find.

The checklist is fixed and lives in code. A model asked to invent the aspects
that matter will produce a different list for every topic, and a gap report whose
criteria move cannot be compared across topics or across weeks — which is the
only way a gap report is useful, because the point is to watch the gaps close.
"""

from paimon.agents.support import load_documents
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
from paimon.domain.ports import ChatModel, DocumentRepository, SearchFilters, TokenCounter
from paimon.rag.citations import resolve_citations
from paimon.rag.prompting import DEFAULT_CONTEXT_TOKENS, build_prompt

AGENT_NAME = "documentation-gaps"

#: What operational documentation is expected to cover. Fixed so that two runs,
#: on two topics or two months apart, are answering the same question.
ASPECTS = (
    "symptoms",
    "detection",
    "mitigation",
    "rollback",
    "ownership",
    "escalation",
)

INSTRUCTION = (
    "Assess how well the sources document this topic: {topic}\n\n"
    "For each of these aspects, say whether the sources cover it, and cite the "
    "source when they do: {aspects}.\n"
    "Where an aspect is not covered, say so plainly and cite nothing for it."
)

NOTHING_INDEXED = (
    "There is no indexed material on that topic at all, which is itself the "
    "finding: every aspect is undocumented."
)


def build_gaps_graph(
    retrieve: RetrieveChunks,
    chat_model: ChatModel,
    repository: DocumentRepository,
    token_counter: TokenCounter,
    *,
    max_context_tokens: int = DEFAULT_CONTEXT_TOKENS,
) -> GraphSpec:
    """Assemble the gap analysis agent.

    Args:
        retrieve: Retrieval, already configured.
        chat_model: Called once, to assess.
        repository: Loads the documents behind cited chunks.
        token_counter: Enforces the context budget.
        max_context_tokens: Budget for the sources section of the prompt.

    Returns:
        A validated graph specification.
    """

    async def survey(state: AgentState) -> StateUpdate:
        """Gather everything the corpus has on the topic."""
        result = await retrieve(state.question, SearchFilters(tenant_id=state.tenant_id))
        return {"evidence": tuple(hit.chunk for hit in result.hits)}

    async def report_nothing(_state: AgentState) -> StateUpdate:
        """An empty corpus is a complete answer, and needs no model.

        Unlike triage, this is not a refusal. "Nothing is documented" is exactly
        what a gap analysis exists to discover, so it is reported as a finding.
        """
        return {"draft": NOTHING_INDEXED, "citations": ()}

    async def analyse(state: AgentState) -> StateUpdate:
        """Ask, once, which aspects the retrieved material covers."""
        instruction = INSTRUCTION.format(topic=state.question, aspects=", ".join(ASPECTS))
        prompt = build_prompt(instruction, state.evidence, token_counter, max_context_tokens)
        completion = await chat_model.complete(list(prompt.messages))
        documents = await load_documents(repository, prompt.sources, state.tenant_id)
        cited = resolve_citations(completion.text, prompt.sources, documents)
        return {"draft": cited.text, "citations": cited.citations}

    async def summarise(state: AgentState) -> StateUpdate:
        """Record which aspects the report mentions, and how much it rests on.

        The aspect count is a presence check over the draft, which is a coarse
        signal and is treated as one: it is reported in the step and gates
        nothing. What does gate is the same rule every agent here applies — a
        report citing nothing is a report about the model, not about the corpus.
        """
        if not state.citations:
            return {"draft": NOTHING_INDEXED, "citations": ()}
        return {"notes": " ".join(_mentioned(state.draft))}

    return GraphSpec(
        name=AGENT_NAME,
        entry="survey",
        nodes=[
            NodeSpec(
                name="survey",
                run=survey,
                summary="surveyed the corpus",
                report=_report_survey,
            ),
            NodeSpec(
                name="report_nothing",
                run=report_nothing,
                summary="found nothing indexed on the topic",
            ),
            NodeSpec(name="analyse", run=analyse, summary="assessed coverage per aspect"),
            NodeSpec(
                name="summarise",
                run=summarise,
                summary="counted the aspects covered",
                report=_report_coverage,
            ),
        ],
        edges=[
            ("report_nothing", END),
            ("analyse", "summarise"),
            ("summarise", END),
        ],
        branches=[
            Branch(
                source="survey",
                decide=lambda state: "analyse" if state.evidence else "nothing",
                targets={"analyse": "analyse", "nothing": "report_nothing"},
            )
        ],
    )


def _mentioned(draft: str) -> tuple[str, ...]:
    """Aspects named anywhere in the draft, in checklist order."""
    lowered = draft.lower()
    return tuple(aspect for aspect in ASPECTS if aspect in lowered)


def _report_survey(_state: AgentState, update: StateUpdate) -> StepReport:
    evidence = update.get("evidence", ())
    documents = len({chunk.document_id for chunk in evidence})
    return StepReport(
        summary=f"surveyed {len(evidence)} chunks across {documents} documents",
        details={"chunks": str(len(evidence)), "documents": str(documents)},
    )


def _report_coverage(state: AgentState, _update: StateUpdate) -> StepReport:
    mentioned = _mentioned(state.draft)
    return StepReport(
        summary=f"{len(mentioned)} of {len(ASPECTS)} aspects addressed",
        details={"addressed": ", ".join(mentioned) or "none"},
    )
