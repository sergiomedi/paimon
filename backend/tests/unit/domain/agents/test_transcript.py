"""The conversation a self-directing agent holds, and the arithmetic of stopping.

Every stop condition the investigator has is decided here, in pure functions over
an immutable value. That is the point of the type existing: these assertions need
no graph, no model and no clock, so the agent's tests are free to be about the
agent rather than about re-deriving when a budget is spent.
"""

import pytest

from paimon.domain.agents import StopReason, Transcript, fingerprint
from paimon.domain.ports import Message, ToolCall

SEARCH = ToolCall(call_id="call-1", name="search_corpus", arguments={"query": "drain", "limit": 5})


def call(call_id: str = "call-1", **arguments: object) -> ToolCall:
    return ToolCall(
        call_id=call_id, name="search_corpus", arguments=arguments or {"query": "drain"}
    )


class TestFingerprint:
    def test_the_same_request_fingerprints_the_same(self) -> None:
        assert fingerprint(call("a", query="drain")) == fingerprint(call("b", query="drain"))

    def test_the_call_id_is_not_part_of_the_identity(self) -> None:
        # Providers mint a fresh id per turn. Including it would make every call
        # unique and the repetition check would never fire once.
        first = ToolCall(call_id="call-1", name="search_corpus", arguments={"query": "drain"})
        second = ToolCall(call_id="call-99", name="search_corpus", arguments={"query": "drain"})
        assert fingerprint(first) == fingerprint(second)

    def test_argument_order_does_not_change_the_identity(self) -> None:
        first = ToolCall(call_id="a", name="search_corpus", arguments={"query": "x", "limit": 5})
        second = ToolCall(call_id="a", name="search_corpus", arguments={"limit": 5, "query": "x"})
        assert fingerprint(first) == fingerprint(second)

    def test_different_arguments_are_different_requests(self) -> None:
        assert fingerprint(call(query="drain")) != fingerprint(call(query="evict"))

    def test_the_tool_name_is_part_of_the_identity(self) -> None:
        search = ToolCall(call_id="a", name="search_corpus", arguments={"x": 1})
        read = ToolCall(call_id="a", name="read_document", arguments={"x": 1})
        assert fingerprint(search) != fingerprint(read)


class TestConversation:
    def test_opening_seeds_the_instructions_and_the_question(self) -> None:
        opened = Transcript().opened("you answer from sources", "why did the drain stall?")
        assert [message.role for message in opened.messages] == ["system", "user"]
        assert opened.messages[1].content == "why did the drain stall?"

    def test_a_turn_is_a_model_call_not_a_message(self) -> None:
        # An assistant turn asking for two tools produces three messages and
        # costs one turn. Counting messages would make the budget depend on how
        # many calls a model happened to batch into one response.
        transcript = Transcript().with_turn(
            Message(role="assistant", content="", tool_calls=(call("a"), call("b")))
        )
        transcript = transcript.with_results(
            [
                Message(role="tool", content="one", tool_call_id="a"),
                Message(role="tool", content="two", tool_call_id="b"),
            ]
        )
        assert transcript.turns == 1
        assert len(transcript.messages) == 3

    def test_the_conversation_keeps_its_order(self) -> None:
        transcript = (
            Transcript()
            .opened("rules", "question")
            .with_turn(Message(role="assistant", content="looking"))
            .with_results([Message(role="tool", content="a passage", tool_call_id="a")])
        )
        assert [message.role for message in transcript.messages] == [
            "system",
            "user",
            "assistant",
            "tool",
        ]


class TestRepetition:
    def test_a_first_call_is_not_a_repeat(self) -> None:
        assert not Transcript().is_repeat(SEARCH)

    def test_the_same_call_again_is_a_repeat(self) -> None:
        transcript = Transcript().with_call(SEARCH)
        assert transcript.is_repeat(SEARCH)

    def test_a_repeat_does_not_enter_the_ledger_twice(self) -> None:
        # The ledger is a set of distinct requests; the counter is a count of
        # events. Conflating them would make the second repeat look like a third.
        transcript = Transcript().with_call(SEARCH).with_call(SEARCH)
        assert len(transcript.seen_calls) == 1
        assert transcript.repeated_calls == 1
        assert transcript.tool_calls == 2

    def test_one_repeat_is_tolerated(self) -> None:
        transcript = Transcript().with_call(SEARCH).with_call(SEARCH)
        assert not transcript.repeating()

    def test_a_second_repeat_is_not(self) -> None:
        transcript = Transcript().with_call(SEARCH).with_call(SEARCH).with_call(SEARCH)
        assert transcript.repeated_calls == 2
        assert transcript.repeating()

    def test_a_failed_call_is_still_a_call(self) -> None:
        transcript = Transcript().with_call(SEARCH, failed=True)
        assert transcript.tool_calls == 1
        assert transcript.tool_errors == 1


class TestStopping:
    def test_a_fresh_transcript_is_running(self) -> None:
        assert not Transcript().stopped
        assert Transcript().stop is StopReason.RUNNING

    def test_ending_fixes_the_reason(self) -> None:
        transcript = Transcript().ended(StopReason.STEP_LIMIT)
        assert transcript.stopped
        assert transcript.stop is StopReason.STEP_LIMIT

    def test_the_first_reason_wins(self) -> None:
        # A pass that exhausts the turn budget while a tool fails stopped for
        # whichever happened first. Overwriting would report the tidier of the
        # two, and the tidier one is rarely the one worth investigating.
        transcript = Transcript().ended(StopReason.RETRIEVAL_FAILED).ended(StopReason.STEP_LIMIT)
        assert transcript.stop is StopReason.RETRIEVAL_FAILED

    def test_only_answering_may_carry_an_answer(self) -> None:
        assert StopReason.ANSWERED.can_answer
        for reason in (
            StopReason.STEP_LIMIT,
            StopReason.TOKEN_BUDGET,
            StopReason.REPEATED_CALL,
            StopReason.RETRIEVAL_FAILED,
            StopReason.NO_MATERIAL,
        ):
            assert not reason.can_answer

    def test_every_stop_reason_the_agent_can_report_is_named(self) -> None:
        # Pins the enumeration itself. A loop that stopped for a reason not on
        # this list has a bug rather than a state, and this is where that claim
        # is kept honest when somebody adds a sixth exit.
        assert {reason.value for reason in StopReason} == {
            "",
            "answered",
            "step_limit",
            "token_budget",
            "repeated_call",
            "retrieval_failed",
            "no_material",
        }


class TestImmutability:
    def test_a_transition_leaves_the_original_alone(self) -> None:
        # What makes it safe to hand a transcript to a node: the node cannot
        # change the caller's copy, so a retried or replayed node starts from
        # what it was actually given.
        before = Transcript().with_call(SEARCH)
        before.with_call(SEARCH).ended(StopReason.REPEATED_CALL)
        assert before.repeated_calls == 0
        assert not before.stopped

    def test_it_cannot_be_written_to(self) -> None:
        with pytest.raises(AttributeError):
            Transcript().turns = 4  # type: ignore[misc]


class TestConcluding:
    def test_it_overwrites_a_reason_ended_would_have_kept(self) -> None:
        # The one case: a loop that believes it answered but gathered nothing
        # did not answer. Deciding that needs the evidence, which the loop never
        # looks at, so it cannot be decided while the loop is running.
        transcript = Transcript().ended(StopReason.ANSWERED)

        assert transcript.concluded(StopReason.NO_MATERIAL).stop is StopReason.NO_MATERIAL

    def test_ending_afterwards_still_does_nothing(self) -> None:
        transcript = Transcript().concluded(StopReason.NO_MATERIAL)

        assert transcript.ended(StopReason.ANSWERED).stop is StopReason.NO_MATERIAL
