"""Which agents exist, and how to build one.

A registry rather than three imports at the composition root, because the set of
agents is a fact about this layer: adding one should mean adding a line here, not
editing the wiring. It stays framework-free, so a caller can enumerate the
agents — for an API listing, or for a benchmark that runs each in turn — without
building any of them.
"""

from collections.abc import Callable, Mapping
from contextlib import suppress

from paimon.agents import gaps, investigator, postmortem, triage
from paimon.agents.collaborators import AgentCollaborators
from paimon.agents.investigator import UnsupportedModelError
from paimon.domain.agents import GraphSpec

GraphBuilder = Callable[[AgentCollaborators], GraphSpec]

AGENTS: Mapping[str, GraphBuilder] = {
    triage.AGENT_NAME: triage.build_triage_graph,
    postmortem.AGENT_NAME: postmortem.build_postmortem_graph,
    gaps.AGENT_NAME: gaps.build_gaps_graph,
    investigator.AGENT_NAME: investigator.build_investigator_graph,
}
"""Every agent the platform offers, by the name its runs are recorded under.

Every builder shares a signature on purpose: one
:class:`~paimon.agents.collaborators.AgentCollaborators`, and whatever options
that agent has as keyword arguments with defaults. That uniformity is what lets
the composition root wire all of them in one loop rather than four special
cases, each of which would be somewhere for them to drift apart.

Offered is not the same as *available*. Whether a deployment can run an agent
can depend on what its model is capable of, which this mapping does not know and
:func:`build_all` does.
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
    investigator.AGENT_NAME: (
        "Given an open operational question, searches and reads its way to an "
        "answer over as many hops as it takes, then cites what it used or "
        "refuses. Needs a model that can call tools."
    ),
}


def build_all(
    collaborators: AgentCollaborators,
    *,
    review_postmortems: bool = False,
    max_turns: int = investigator.DEFAULT_MAX_TURNS,
    token_budget: int = investigator.DEFAULT_TOKEN_BUDGET,
    variants: bool = False,
) -> dict[str, GraphSpec]:
    """Build every agent this deployment can run, applying its options.

    Written out rather than looped over :data:`AGENTS`, because the agents have
    options and a loop that pretended otherwise would thread every option
    through a signature most of them do not use. A test keeps this in step with
    the registry so the two cannot drift.

    Args:
        collaborators: The ports and use cases the agents' nodes call.
        review_postmortems: Suspend a postmortem run for a reviewer before it is
            finalised.
        max_turns: Model turns the investigator may take in one run.
        token_budget: Tokens the investigator may spend in one run.
        variants: Also build the investigator as it was first measured, so the
            change to how it presents passages can be compared against what it
            replaced. **For the benchmark only** — the API serves the four
            agents in :data:`AGENTS`, and an operator has no reason to run a
            version the project has already moved on from.

    Returns:
        Each available agent's validated graph, by name. The investigator is
        **absent** when the configured model cannot call tools; use
        :func:`unavailable` to find out why, rather than inferring it.
    """
    built = {
        triage.AGENT_NAME: triage.build_triage_graph(collaborators),
        postmortem.AGENT_NAME: postmortem.build_postmortem_graph(
            collaborators, review=review_postmortems
        ),
        gaps.AGENT_NAME: gaps.build_gaps_graph(collaborators),
    }
    # Suppressed rather than handled, and it is not an error. A deployment on a
    # model without tool calling is a supported deployment — it is most of what
    # this platform runs locally — and it gets three agents instead of four.
    # What it must not get is a fourth agent that fails on its first question,
    # which is what registering it anyway would mean. `unavailable` is how a
    # caller finds out, so nothing is being swallowed.
    with suppress(UnsupportedModelError):
        built[investigator.AGENT_NAME] = investigator.build_investigator_graph(
            collaborators,
            max_turns=max_turns,
            token_budget=token_budget,
        )
        if variants:
            built[investigator.V1_AGENT_NAME] = investigator.build_investigator_graph(
                collaborators,
                max_turns=max_turns,
                token_budget=token_budget,
                layout=investigator.PassageFormat.TOOL_LINES,
            )
    return built


def unavailable(collaborators: AgentCollaborators) -> dict[str, str]:
    """Which registered agents this deployment cannot run, and why.

    Returned rather than logged, so the API can say "this deployment runs a
    model that cannot call tools" instead of "no agent named 'investigator'".
    The second is true and useless: it reads as a typo, and the person who sent
    it will look for the typo.

    Args:
        collaborators: The ports and use cases the agents' nodes call.

    Returns:
        A reason per unavailable agent. Empty when every agent can run.
    """
    try:
        investigator.build_investigator_graph(collaborators)
    except UnsupportedModelError as error:
        return {investigator.AGENT_NAME: str(error)}
    return {}
