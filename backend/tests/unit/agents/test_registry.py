"""What this deployment offers, and what only the benchmark may reach.

The registry answers one question — which agents can this process run — and it
answers it twice, because the two callers want different things. The API wants
the agents the platform stands behind. The benchmark wants those *and* the
version a change replaced, so the change can be reported as a number rather
than asserted.
"""

from tests.fakes.scripted_tool_chat import ScriptedToolCallingChatModel
from tests.unit.agents.conftest import Harness

from paimon.agents import AGENTS, build_all
from paimon.agents.investigator import AGENT_NAME, V1_AGENT_NAME


def tool_calling() -> Harness:
    """A harness whose model can call tools, so all four agents build."""
    harness = Harness()
    harness.chat_model = ScriptedToolCallingChatModel(turns=())  # type: ignore[assignment]
    return harness


class TestWhichAgentsGetBuilt:
    def test_the_registered_agents_are_built_by_default(self) -> None:
        built = build_all(tool_calling().collaborators())

        assert set(built) == set(AGENTS)

    def test_the_superseded_investigator_is_not_among_them(self) -> None:
        # The default path feeds the API. An operator has no reason to run a
        # version the project has measured and moved on from, and offering one
        # would make "which investigator answered this?" a question a caller
        # has to ask.
        built = build_all(tool_calling().collaborators())

        assert V1_AGENT_NAME not in built

    def test_the_benchmark_can_ask_for_it_by_name(self) -> None:
        built = build_all(tool_calling().collaborators(), variants=True)

        assert V1_AGENT_NAME in built
        assert AGENT_NAME in built

    def test_asking_for_it_adds_nothing_else(self) -> None:
        # A variants flag that quietly changed the other agents would make the
        # comparison meaningless: the held-out run puts both investigators in
        # one process against one corpus, and everything except the version
        # under test has to be the same object it would otherwise be.
        default = build_all(tool_calling().collaborators())
        with_variants = build_all(tool_calling().collaborators(), variants=True)

        assert set(with_variants) - set(default) == {V1_AGENT_NAME}

    def test_it_is_not_described_to_callers(self) -> None:
        # `AGENTS` is what the listing endpoint renders. A name in the registry
        # and not in `AGENTS` is a name no client is told about, which is
        # exactly the status this one should have.
        assert V1_AGENT_NAME not in AGENTS


class TestWhatEachVariantCallsItself:
    """A run record, a report and a calibration case id all name their system.

    Two graphs sharing one name is not cosmetic: the paired comparison labels
    both sides identically, and `attempt_id` builds `system/task/trial`, so the
    two runs' attempts collide in any sample drawn across them.
    """

    def test_the_default_build_is_named_for_the_agent(self) -> None:
        built = build_all(tool_calling().collaborators(), variants=True)

        assert built[AGENT_NAME].name == AGENT_NAME

    def test_the_superseded_build_names_itself_apart(self) -> None:
        built = build_all(tool_calling().collaborators(), variants=True)

        assert built[V1_AGENT_NAME].name == V1_AGENT_NAME

    def test_every_registered_name_matches_the_graph_it_builds(self) -> None:
        # The registry key is what the CLI resolves; the graph's own name is
        # what gets recorded. A benchmark that asks for one and records the
        # other is a benchmark whose output cannot be attributed.
        built = build_all(tool_calling().collaborators(), variants=True)

        assert all(name == spec.name for name, spec in built.items())
