"""The agents themselves: node bodies and the graphs they compose into.

Framework-free by contract (ADR-0015). The vocabulary these are written in — the
state, the graph description — lives in :mod:`paimon.domain.agents`, because the
orchestration adapter has to speak it too.
"""
