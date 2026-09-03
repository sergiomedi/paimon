"""Which agents exist, and how to build one.

A registry rather than three imports at the composition root, because the set of
agents is a fact about this layer: adding one should mean adding a line here, not
editing the wiring. It stays framework-free, so a caller can enumerate the
agents — for an API listing, or for a benchmark that runs each in turn — without
building any of them.
"""

from collections.abc import Callable, Mapping

from paimon.agents import gaps, postmortem, triage
from paimon.agents.collaborators import AgentCollaborators
from paimon.domain.agents import GraphSpec

GraphBuilder = Callable[[AgentCollaborators], GraphSpec]

AGENTS: Mapping[str, GraphBuilder] = {
    triage.AGENT_NAME: triage.build_triage_graph,
    postmortem.AGENT_NAME: postmortem.build_postmortem_graph,
    gaps.AGENT_NAME: gaps.build_gaps_graph,
}
"""Every agent the platform offers, by the name its runs are recorded under.

The three builders share a signature on purpose: one
:class:`~paimon.agents.collaborators.AgentCollaborators`, and whatever options
that agent has as keyword arguments with defaults. That uniformity is what lets
the composition root wire all of them in one loop rather than three special
cases, each of which would be somewhere for them to drift apart.
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


def build_all(
    collaborators: AgentCollaborators, *, review_postmortems: bool = False
) -> dict[str, GraphSpec]:
    """Build every agent, applying the options a deployment has chosen.

    Written out rather than looped over :data:`AGENTS`, because exactly one agent
    has an option and a loop that pretended otherwise would need the options
    threaded through a signature none of the others use. One agent being special
    is cheaper to state than to abstract; a test keeps this in step with the
    registry so the two cannot drift.

    Args:
        collaborators: The ports and use cases the agents' nodes call.
        review_postmortems: Suspend a postmortem run for a reviewer before it is
            finalised.

    Returns:
        Each agent's validated graph, by name.
    """
    return {
        triage.AGENT_NAME: triage.build_triage_graph(collaborators),
        postmortem.AGENT_NAME: postmortem.build_postmortem_graph(
            collaborators, review=review_postmortems
        ),
        gaps.AGENT_NAME: gaps.build_gaps_graph(collaborators),
    }
