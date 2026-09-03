"""An in-memory AgentCheckpointer.

Not only a test double: it is the reference implementation the contract suite is
written against, and it is what a deployment uses when durability is not wanted
(a single-process demo, an evaluation run). Passing the same assertions as the
PostgreSQL adapter is what makes it safe to substitute.
"""

from collections.abc import Sequence

from paimon.domain.entities import AgentRun


class InMemoryCheckpointer:
    """Keeps runs in a dictionary, newest first on listing."""

    def __init__(self) -> None:
        """Start empty."""
        self._runs: dict[str, AgentRun] = {}

    async def save(self, run: AgentRun) -> None:
        """Record a run, replacing any earlier state for the same thread."""
        self._runs[run.thread_id] = run

    async def load(self, thread_id: str) -> AgentRun | None:
        """Return a run, or None when the thread is unknown."""
        return self._runs.get(thread_id)

    async def list_runs(self, tenant_id: str, *, limit: int = 50) -> Sequence[AgentRun]:
        """Return a tenant's runs, most recently started first."""
        owned = [run for run in self._runs.values() if run.tenant_id == tenant_id]
        owned.sort(key=lambda run: run.started_at, reverse=True)
        return owned[:limit]
