"""The vocabulary for describing an agent: its state, and the shape of its graph.

In the domain rather than in :mod:`paimon.agents` because these are the types the
:class:`~paimon.domain.ports.AgentWorkflow` port is written in, and an adapter
implementing that port has to speak them. Putting them any higher would force
infrastructure to import a layer above it, which is the one direction the
dependency rule does not allow.

The distinction this draws is worth stating: these types describe *how an agent
is described*; :mod:`paimon.agents` holds the agents themselves — the node bodies
and the graphs they compose into. Neither imports an orchestration framework
(ADR-0015).
"""

from paimon.domain.agents.graph import (
    END,
    Branch,
    GraphSpec,
    Node,
    NodeSpec,
    StepReport,
)
from paimon.domain.agents.state import (
    AgentState,
    StateUpdate,
    append_steps,
    merge_evidence,
)

__all__ = [
    "END",
    "AgentState",
    "Branch",
    "GraphSpec",
    "Node",
    "NodeSpec",
    "StateUpdate",
    "StepReport",
    "append_steps",
    "merge_evidence",
]
