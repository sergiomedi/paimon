"""Which agents exist, and how to build one.

A registry rather than three imports at the composition root, because the set of
agents is a fact about this layer: adding one should mean adding a line here, not
editing the wiring. It stays framework-free, so a caller can enumerate the
agents — for an API listing, or for a benchmark that runs each in turn — without
building any of them.
"""

from collections.abc import Callable, Mapping

from paimon.agents import gaps, postmortem, triage
from paimon.application.use_cases.retrieve_chunks import RetrieveChunks
from paimon.domain.agents import GraphSpec
from paimon.domain.ports import ChatModel, DocumentRepository, TokenCounter

GraphBuilder = Callable[
    [RetrieveChunks, ChatModel, DocumentRepository, TokenCounter],
    GraphSpec,
]

AGENTS: Mapping[str, GraphBuilder] = {
    triage.AGENT_NAME: triage.build_triage_graph,
    postmortem.AGENT_NAME: postmortem.build_postmortem_graph,
    gaps.AGENT_NAME: gaps.build_gaps_graph,
}
"""Every agent the platform offers, by the name its runs are recorded under.

The three builders share a signature on purpose. It is the smallest set of
collaborators any of them needs, and keeping it uniform is what lets the
composition root wire all of them in one loop rather than three special cases —
each of which would be somewhere for them to drift apart.
"""

AGENT_DESCRIPTIONS: Mapping[str, str] = {
    triage.AGENT_NAME: (
        "Given a symptom, searches runbooks for a procedure and postmortems for "
        "precedent, and answers with citations or not at all."
    ),
    postmortem.AGENT_NAME: (
        "Given an incident timeline, drafts a postmortem grounded in the timeline "
        "itself and in comparable earlier incidents."
    ),
    gaps.AGENT_NAME: (
        "Given a topic, reports which operational aspects the corpus documents "
        "and which it leaves undocumented."
    ),
}
