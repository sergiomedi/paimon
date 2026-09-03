"""The agents themselves: node bodies and the graphs they compose into.

Framework-free by contract (ADR-0015). The vocabulary these are written in — the
state, the graph description — lives in :mod:`paimon.domain.agents`, because the
orchestration adapter has to speak it too.
"""

from paimon.agents.collaborators import AgentCollaborators
from paimon.agents.registry import AGENT_DESCRIPTIONS, AGENTS, GraphBuilder, build_all

__all__ = ["AGENTS", "AGENT_DESCRIPTIONS", "AgentCollaborators", "GraphBuilder", "build_all"]
