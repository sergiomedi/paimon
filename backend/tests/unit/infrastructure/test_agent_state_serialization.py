"""What survives being written into a graph checkpoint, and what is refused.

The allowlist in ``serde`` closes a real hole — reviving an arbitrary class out
of a checkpoint is code execution for anyone who can write to that database — and
the cost of closing it is that a type added to the state and forgotten here fails
at *resume* time, on a run somebody was waiting for, inside framework frames.

So these round-trip what a checkpoint actually holds. A resumable investigator
run checkpoints its whole conversation, and a conversation that cannot be revived
is a run that cannot be continued.
"""

from datetime import UTC, datetime
from typing import Any

import pytest

from paimon.domain.agents import SeenCall, StopReason, Transcript
from paimon.domain.entities import AgentStep, Chunk, RunStatus
from paimon.domain.ports import Message, ToolCall
from paimon.domain.value_objects import Citation
from paimon.infrastructure.orchestration import build_serializer
from paimon.infrastructure.orchestration.serde import ALLOWED_MODULES

CHUNK = Chunk(
    chunk_id="doc-1:0",
    document_id="doc-1",
    tenant_id="tenant-a",
    ordinal=0,
    text="Cordon the node first.",
    start_char=0,
    end_char=22,
    token_count=5,
)


def round_trip(value: object) -> Any:
    """Write a value the way a checkpoint does, and read it back.

    Returns ``Any`` rather than ``object`` on purpose: what comes back is
    genuinely of unknown type — that is the whole question these tests ask — and
    annotating the honest answer keeps the assertions readable instead of
    scattered with ignores that would hide the next real mismatch.
    """
    serializer = build_serializer()
    return serializer.loads_typed(serializer.dumps_typed(value))


class TestTranscript:
    def test_a_conversation_survives_a_checkpoint(self) -> None:
        transcript = (
            Transcript()
            .opened("you answer from sources", "why did the drain stall?")
            .with_turn(
                Message(
                    role="assistant",
                    content="",
                    tool_calls=(
                        ToolCall(
                            call_id="call-1",
                            name="search_corpus",
                            arguments={"query": "drain", "limit": 5},
                        ),
                    ),
                )
            )
            .with_results([Message(role="tool", content="[1] a passage", tool_call_id="call-1")])
        )

        revived = round_trip(transcript)

        assert isinstance(revived, Transcript)
        assert [(message.role, message.content) for message in revived.messages] == [
            ("system", "you answer from sources"),
            ("user", "why did the drain stall?"),
            ("assistant", ""),
            ("tool", "[1] a passage"),
        ]
        assert revived.messages[2].tool_calls[0].name == "search_corpus"
        assert revived.messages[3].tool_call_id == "call-1"

    def test_the_counters_and_the_stop_reason_survive(self) -> None:
        transcript = (
            Transcript()
            .with_call(ToolCall(call_id="c", name="search_corpus", arguments={"q": "x"}))
            .ended(StopReason.TOKEN_BUDGET)
        )

        revived = round_trip(transcript)

        assert isinstance(revived, Transcript)
        assert revived.tool_calls == 1
        assert revived.stop is StopReason.TOKEN_BUDGET

    def test_a_tool_call_keeps_its_arguments(self) -> None:
        # The thing a repeat check is computed from. Arguments that came back as
        # something else would fingerprint differently, and a resumed run would
        # stop recognising calls it had already made.
        call = ToolCall(call_id="c-1", name="search_corpus", arguments={"query": "x", "limit": 5})

        revived = round_trip(call)

        assert revived == call

    def test_a_revived_repeat_is_still_recognised_as_one(self) -> None:
        # The end-to-end version of the test above, and the one that matters: a
        # run resumed from a checkpoint must not forget which calls it has
        # already made, or the repetition stop condition silently stops working
        # for exactly the runs that were interrupted.
        call = ToolCall(call_id="c-1", name="search_corpus", arguments={"query": "x", "limit": 5})
        revived = round_trip(Transcript().with_call(call))

        assert isinstance(revived, Transcript)
        assert revived.is_repeat(
            ToolCall(call_id="c-2", name="search_corpus", arguments={"query": "x", "limit": 5})
        )


#: One live instance of every type on the allowlist, built with its tuple
#: fields populated — the fields an encoding with a single sequence type is
#: exactly what breaks. Keyed by the (module, name) pair the allowlist uses, so
#: the catalogue test below can prove it covers all of them rather than claiming
#: to.
SPECIMENS: dict[tuple[str, str], object] = {
    ("paimon.domain.entities.document", "Chunk"): CHUNK,
    ("paimon.domain.entities.agent", "AgentStep"): AgentStep(
        name="act",
        summary="called the model",
        started_at=datetime(2026, 9, 23, 10, 0, tzinfo=UTC),
        finished_at=datetime(2026, 9, 23, 10, 0, 1, tzinfo=UTC),
        input_tokens=100,
        output_tokens=20,
        details={"turn": "1", "requested": "search_corpus"},
    ),
    ("paimon.domain.entities.agent", "RunStatus"): RunStatus.AWAITING_INPUT,
    ("paimon.domain.value_objects.citation", "Citation"): Citation(
        marker=1,
        document_id="doc-1",
        chunk_id="doc-1:0",
        source_uri="doc-1.md",
        title="Node maintenance",
        heading_path=("Node maintenance", "Draining"),
        start_char=0,
        end_char=22,
        quote="Cordon the node first.",
    ),
    ("paimon.domain.ports.chat", "ToolCall"): ToolCall(
        call_id="call-1", name="search_corpus", arguments={"query": "drain", "limit": 5}
    ),
    ("paimon.domain.ports.chat", "Message"): Message(
        role="assistant",
        content="looking",
        tool_calls=(
            ToolCall(call_id="call-1", name="search_corpus", arguments={"query": "drain"}),
        ),
    ),
    ("paimon.domain.agents.transcript", "SeenCall"): SeenCall(fingerprint="{}", markers=(3, 4)),
    ("paimon.domain.agents.transcript", "StopReason"): StopReason.REPEATED_CALL,
    ("paimon.domain.agents.transcript", "Transcript"): (
        Transcript()
        .opened("rules", "why did the drain stall?")
        .with_turn(
            Message(
                role="assistant",
                content="",
                tool_calls=(
                    ToolCall(call_id="c1", name="search_corpus", arguments={"query": "drain"}),
                ),
            )
        )
        .with_call(
            ToolCall(call_id="c1", name="search_corpus", arguments={"query": "drain"}), (1, 2)
        )
        .with_results([Message(role="tool", content="[1] a passage", tool_call_id="c1")])
        .ended(StopReason.ANSWERED)
    ),
}


class TestTheWholeCatalogue:
    """Every type on the allowlist, stored and read back, required to be equal.

    A catalogue rather than a test per type, because the failure this guards
    against is *omission*: a type added to the state and to the allowlist, and
    never round-tripped, which then comes back subtly different on the first
    resumed run in production.

    Equality is the assertion, not "the fields I remembered to check". JSON has
    one sequence type, so every tuple on these types was coming back a list —
    ``Chunk.heading_path`` most consequentially — and a revived chunk therefore
    compared unequal to the chunk that was stored. Nothing shallow would have
    caught it.
    """

    @pytest.mark.parametrize("entry", ALLOWED_MODULES, ids=lambda entry: f"{entry[0]}.{entry[1]}")
    def test_every_allowlisted_type_survives_exactly(self, entry: tuple[str, str]) -> None:
        specimen = SPECIMENS[entry]

        assert round_trip(specimen) == specimen

    @pytest.mark.parametrize("entry", ALLOWED_MODULES, ids=lambda entry: f"{entry[0]}.{entry[1]}")
    def test_every_allowlisted_type_keeps_its_own_type(self, entry: tuple[str, str]) -> None:
        specimen = SPECIMENS[entry]

        assert type(round_trip(specimen)) is type(specimen)

    def test_the_catalogue_covers_the_allowlist(self) -> None:
        # The assertion that keeps the two tests above honest. Without it,
        # adding a type to the allowlist and forgetting to add a specimen would
        # silently test one fewer type than the allowlist claims to protect.
        assert set(SPECIMENS) == set(ALLOWED_MODULES)


class TestTheTuplesComeBack:
    """The specific repair, named, because it was a real defect.

    Checked on the two types whose tuple field is load-bearing rather than
    incidental: a heading path is what a citation is displayed under, and a
    conversation is what a resumed loop continues from.
    """

    def test_a_chunks_heading_path_is_a_tuple_again(self) -> None:
        revived = round_trip(CHUNK)

        assert isinstance(revived.heading_path, tuple)
        assert revived.heading_path == CHUNK.heading_path

    def test_a_citations_heading_path_is_a_tuple_again(self) -> None:
        citation = SPECIMENS[("paimon.domain.value_objects.citation", "Citation")]

        revived = round_trip(citation)

        assert isinstance(revived.heading_path, tuple)

    def test_a_transcript_is_restored_all_the_way_down(self) -> None:
        # Three levels: the transcript's messages, a message's tool calls, and a
        # seen call's markers. A repair that stopped at the top level would put
        # the outer tuple back and leave the inner ones lists.
        transcript = SPECIMENS[("paimon.domain.agents.transcript", "Transcript")]

        revived = round_trip(transcript)

        assert isinstance(revived.messages, tuple)
        assert isinstance(revived.messages[2].tool_calls, tuple)
        assert isinstance(revived.seen_calls, tuple)
        assert isinstance(revived.seen_calls[0].markers, tuple)

    def test_a_list_that_was_always_a_list_is_left_alone(self) -> None:
        # Nothing in the payload says whether a bare sequence was written as a
        # list or a tuple, so guessing would turn every list the platform
        # legitimately stores into something else.
        assert round_trip(["a", "b"]) == ["a", "b"]

    def test_a_mapping_field_is_not_turned_into_anything(self) -> None:
        step = SPECIMENS[("paimon.domain.entities.agent", "AgentStep")]

        assert round_trip(step).details == {"turn": "1", "requested": "search_corpus"}


class TestWhatAChannelActuallyHolds:
    """The values a checkpoint really contains.

    LangGraph gives a dataclass state schema one channel per field and stores the
    channel values, not the dataclass. So ``AgentState`` is deliberately *not* on
    the allowlist and round-tripping one would be testing a thing that never
    happens; what has to survive is each field's value, which is what these do.
    """

    def test_the_conversation_channel_survives(self) -> None:
        transcript = Transcript().opened("rules", "why did the drain stall?")

        revived = round_trip(transcript)

        assert isinstance(revived, Transcript)
        assert revived.messages[1].content == "why did the drain stall?"

    def test_the_evidence_channel_survives(self) -> None:
        revived = round_trip((CHUNK,))

        assert [chunk.chunk_id for chunk in revived] == ["doc-1:0"]

    def test_the_citations_channel_survives(self) -> None:
        citation = Citation(
            marker=1,
            document_id="doc-1",
            chunk_id="doc-1:0",
            source_uri="doc-1.md",
            title="Node maintenance",
            heading_path=(),
            start_char=0,
            end_char=22,
            quote="Cordon the node first.",
        )

        revived = round_trip((citation,))

        assert revived[0].start_char == 0

    def test_the_steps_channel_survives(self) -> None:
        step = AgentStep(
            name="act",
            summary="called the model",
            started_at=datetime(2026, 9, 23, 10, 0, tzinfo=UTC),
            finished_at=datetime(2026, 9, 23, 10, 0, 1, tzinfo=UTC),
            input_tokens=100,
            output_tokens=20,
        )

        revived = round_trip((step,))

        assert revived[0].total_tokens == 120

    def test_the_state_itself_is_not_on_the_allowlist(self) -> None:
        # Stated as a test rather than left as an absence, because "we forgot"
        # and "it is never serialized" look identical in a list of four entries.
        assert ("paimon.domain.agents.state", "AgentState") not in ALLOWED_MODULES


class TestTheAllowlistIsStillAList:
    def test_a_type_nobody_allowed_does_not_come_back_as_itself(self) -> None:
        # The property the allowlist exists for. Not "it raises" — the
        # serializer's contract is that it will not *reconstruct* an arbitrary
        # class, and asserting the exception type would pin a framework detail
        # rather than the guarantee.
        class Smuggled:
            def __init__(self) -> None:
                self.payload = "arbitrary"

        try:
            revived = round_trip(Smuggled())
        except Exception:  # noqa: BLE001  refusing outright is also a pass
            return
        assert not isinstance(revived, Smuggled)


@pytest.mark.parametrize(
    "module_and_name",
    [
        ("paimon.domain.agents.transcript", "Transcript"),
        ("paimon.domain.agents.transcript", "StopReason"),
        ("paimon.domain.ports.chat", "Message"),
        ("paimon.domain.ports.chat", "ToolCall"),
    ],
)
def test_the_conversation_types_are_on_the_allowlist(module_and_name: tuple[str, str]) -> None:
    assert module_and_name in ALLOWED_MODULES
