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

from paimon.domain.agents import StopReason, Transcript
from paimon.domain.entities import AgentStep, Chunk
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


class TestWhatACheckpointDoesNotPreserve:
    """A property of the framework, recorded so nobody rediscovers it.

    The serializer revives a tuple as a **list**. The dataclasses here declare
    tuples and the type checker believes them, so a revived value is a type the
    annotation says it is not, and ``revived == original`` is false for reasons
    that have nothing to do with the data.

    This is not new and it is not ours — it is true of ``evidence``, ``steps``
    and ``heading_path`` as much as of a conversation. It matters here only
    because the transitions on :class:`Transcript` all rebuild their containers
    with ``(*existing, new)``, which produces a tuple whatever it was handed. So
    the type heals on the first write after a resume, and nothing downstream
    depends on the container's identity. That is the property being pinned.
    """

    def test_a_tuple_comes_back_as_a_list(self) -> None:
        revived = round_trip(Transcript().opened("rules", "question"))

        assert isinstance(revived, Transcript)
        # mypy calls this unreachable, and it is right to: the annotation says
        # tuple and a list cannot be one. That disagreement between the
        # annotation and the runtime value *is* the finding, so the ignore is
        # the assertion rather than a way around it.
        assert isinstance(revived.messages, list)  # type: ignore[unreachable]

    def test_the_next_transition_restores_the_tuple(self) -> None:
        revived = round_trip(Transcript().opened("rules", "question"))
        assert isinstance(revived, Transcript)

        continued = revived.with_turn(Message(role="assistant", content="looking"))

        assert isinstance(continued.messages, tuple)
        assert len(continued.messages) == 3


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
