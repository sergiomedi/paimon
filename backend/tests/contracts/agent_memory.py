"""Contract for the AgentMemory port.

Behaviour, not retrieval quality. These assertions fix what any store must do —
namespaces isolate, keys replace, recall is ordered by relevance and bounded by a
limit — and say nothing about how good the ranking is. Ranking quality is what
the Phase 6 benchmark measures, and asserting it here would make every contract
test a hostage to the embedding model.
"""

import pytest

from paimon.domain.errors import AgentMemoryError
from paimon.domain.ports import AgentMemory

INCIDENTS = ("incidents",)
RUNBOOKS = ("runbooks",)


class AgentMemoryContract:
    """Every AgentMemory adapter must pass these."""

    @pytest.fixture
    def memory(self) -> AgentMemory:
        """Supplied by the subclass, empty and ready to write to."""
        raise NotImplementedError

    async def test_what_was_remembered_can_be_recalled(self, memory: AgentMemory) -> None:
        await memory.remember(
            INCIDENTS, "inc-2451", {"summary": "a drain stalled on a disruption budget"}
        )
        recalled = await memory.recall(INCIDENTS, "drain stalled")
        assert [item["summary"] for item in recalled] == ["a drain stalled on a disruption budget"]

    async def test_an_empty_namespace_recalls_nothing_rather_than_failing(
        self, memory: AgentMemory
    ) -> None:
        assert list(await memory.recall(INCIDENTS, "anything")) == []

    async def test_namespaces_do_not_leak_into_one_another(self, memory: AgentMemory) -> None:
        await memory.remember(INCIDENTS, "inc-1", {"summary": "a drain stalled"})
        await memory.remember(RUNBOOKS, "rb-1", {"summary": "a drain stalled"})
        recalled = await memory.recall(INCIDENTS, "drain stalled")
        assert len(recalled) == 1

    async def test_remembering_the_same_key_replaces_it(self, memory: AgentMemory) -> None:
        await memory.remember(INCIDENTS, "inc-1", {"summary": "first account"})
        await memory.remember(INCIDENTS, "inc-1", {"summary": "corrected account"})
        recalled = await memory.recall(INCIDENTS, "account")
        assert [item["summary"] for item in recalled] == ["corrected account"]

    async def test_the_whole_value_survives_the_round_trip(self, memory: AgentMemory) -> None:
        await memory.remember(
            INCIDENTS,
            "inc-1",
            {"summary": "a drain stalled", "resolution": "relaxed the budget"},
        )
        recalled = await memory.recall(INCIDENTS, "drain")
        assert recalled[0]["resolution"] == "relaxed the budget"

    async def test_recall_respects_the_limit(self, memory: AgentMemory) -> None:
        for index in range(5):
            await memory.remember(INCIDENTS, f"inc-{index}", {"summary": f"incident {index}"})
        assert len(await memory.recall(INCIDENTS, "incident", limit=2)) == 2

    async def test_an_empty_query_recalls_nothing(self, memory: AgentMemory) -> None:
        # Not an error: an agent with nothing to ask about should get nothing
        # back, rather than the arbitrary first few memories in the store.
        await memory.remember(INCIDENTS, "inc-1", {"summary": "a drain stalled"})
        assert list(await memory.recall(INCIDENTS, "   ")) == []

    async def test_a_memory_with_no_text_is_refused(self, memory: AgentMemory) -> None:
        with pytest.raises(AgentMemoryError, match="cannot be recalled"):
            await memory.remember(INCIDENTS, "inc-1", {"summary": "   "})

    async def test_a_nested_namespace_is_distinct_from_its_prefix(
        self, memory: AgentMemory
    ) -> None:
        await memory.remember(("incidents",), "inc-1", {"summary": "a drain stalled"})
        await memory.remember(("incidents", "network"), "inc-2", {"summary": "a drain stalled"})
        assert len(await memory.recall(("incidents",), "drain")) == 1
