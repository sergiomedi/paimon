"""What an agent that talks to itself has to remember between turns.

The three agents of Phase 3 keep nothing between nodes but evidence and a draft,
because their control flow is a fixed graph and a node never needs to know what
an earlier node said. An agent that decides its own next step does: the model is
shown the whole conversation each turn, so the conversation *is* state.

This is that state, as one value rather than five fields on
:class:`~paimon.domain.agents.state.AgentState`. The reason is the one
:class:`~paimon.agents.collaborators.AgentCollaborators` was extracted for — five
loosely related fields appearing together every time are a concept nobody has
named — and it buys two things beyond tidiness. The transitions below are pure
functions over an immutable value, so every stop condition can be asserted
without a graph; and the budget arithmetic lives next to the counters it reads
rather than inside a node body.

Nothing here calls a model, runs a tool or reads a clock. Deciding *whether* to
stop is arithmetic; doing the thing that consumes the budget is not.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import ClassVar

from paimon.domain.ports.chat import Message, ToolCall


class StopReason(StrEnum):
    """Why a loop stopped.

    Recorded on the run rather than inferred from the trace, because the
    difference between "it answered" and "it ran out of turns while answering"
    is invisible in a transcript that ends either way with an assistant message.
    Every published account of agent design says a loop needs explicit exit
    conditions; this is the enumeration of them, and an agent that stopped for a
    reason not on this list has a bug rather than a state.
    """

    RUNNING = ""
    """Not a stop. The empty string so that ``if transcript.stop`` reads as
    "has it stopped", and so an unset field deserializes to something with a
    name rather than to ``None``."""

    ANSWERED = "answered"
    """The model produced text and asked for nothing further. The only stop that
    can carry an answer."""

    STEP_LIMIT = "step_limit"
    """The turn budget ran out."""

    TOKEN_BUDGET = "token_budget"  # noqa: S105  a budget, not a credential
    """The token budget ran out."""

    REPEATED_CALL = "repeated_call"
    """The model asked for the same thing with the same arguments once too
    often. Warned the first time, stopped the second: a model that repeats
    itself twice after being told it already has the passages is looping, not
    thinking."""

    RETRIEVAL_FAILED = "retrieval_failed"
    """A tool could not run. Deliberately distinct from finding nothing: "I
    looked and the corpus is silent" and "nobody looked" are different
    statements about the world, and only one of them is about the corpus."""

    NO_MATERIAL = "no_material"
    """The loop ended having gathered no evidence at all. Overrides
    :attr:`ANSWERED`: an answer resting on nothing retrieved is an answer from
    parametric memory, whatever the model believed it was doing."""

    @property
    def can_answer(self) -> bool:
        """Whether a run stopped for this reason may carry an answer."""
        return self is StopReason.ANSWERED


def fingerprint(call: ToolCall) -> str:
    """Identify a tool call by what it asks for, not by when it was asked.

    The call id is deliberately excluded: providers mint a fresh one per turn, so
    including it would make every call unique and the repetition check would
    never fire. Arguments are sorted, so ``{"query": "x", "limit": 5}`` and
    ``{"limit": 5, "query": "x"}`` are recognised as the same request — which
    they are, and which a model re-serializing its own history will produce.
    """
    return json.dumps(
        {"name": call.name, "arguments": dict(call.arguments)}, sort_keys=True, default=str
    )


@dataclass(frozen=True, slots=True)
class Transcript:
    """The conversation a loop is holding, and what it has cost so far.

    Immutable, and every transition returns a new one. A mutable transcript
    threaded through graph nodes would be a shared object two concurrent nodes
    could both append to, and the orchestrator's answer to two writes in one step
    is to refuse the run.

    Attributes:
        messages: The conversation as the model will next see it, oldest first.
        turns: Model calls made. Counted rather than derived from ``messages``,
            because an assistant turn that asks for two tools produces three
            messages and one turn, and conflating them would make the budget
            depend on how many tools a model happened to batch.
        seen_calls: Fingerprints of every tool call already made.
        tool_calls: How many tool calls have been executed.
        tool_errors: Calls that came back as an error the model was asked to
            correct. Reported rather than fatal — Anthropic's guidance is that a
            tool error is information for the model — and counted, because a run
            that spent half its turns fixing its own arguments is a run whose
            tool descriptions are wrong.
        repeated_calls: Calls whose fingerprint had been seen before. The first
            is answered with a reminder of what the run already holds; the
            second stops it.
        stop: Why the loop ended, once it has.
    """

    messages: tuple[Message, ...] = ()
    turns: int = 0
    seen_calls: tuple[str, ...] = ()
    tool_calls: int = 0
    tool_errors: int = 0
    repeated_calls: int = 0
    stop: StopReason = StopReason.RUNNING

    #: Repetitions tolerated before the loop is stopped. One, so the model gets
    #: exactly one chance to be told it already has the material and to do
    #: something else with the turn. A ClassVar rather than a field: it is a
    #: property of the design, not of a particular conversation, and a field
    #: would be a knob nobody turns that every checkpoint has to carry.
    TOLERATED_REPEATS: ClassVar[int] = 1

    @property
    def stopped(self) -> bool:
        """Whether a stop reason has been decided."""
        return self.stop is not StopReason.RUNNING

    def opened(self, system: str, question: str) -> "Transcript":
        """Seed a fresh conversation with the instructions and the question.

        The system message is a constant supplied by the agent and the question
        is the caller's. Nothing retrieved goes in either: material reaches the
        model as ``tool`` messages and only as ``tool`` messages, which is the
        boundary that makes a document unable to issue instructions.
        """
        return replace(
            self,
            messages=(
                Message(role="system", content=system),
                Message(role="user", content=question),
            ),
        )

    def with_turn(self, message: Message) -> "Transcript":
        """Record what the model said, and that a turn was spent saying it."""
        return replace(self, messages=(*self.messages, message), turns=self.turns + 1)

    def with_results(self, results: Sequence[Message]) -> "Transcript":
        """Append the tool results the model will see next turn."""
        return replace(self, messages=(*self.messages, *results))

    def is_repeat(self, call: ToolCall) -> bool:
        """Whether this exact request has already been made in this run."""
        return fingerprint(call) in self.seen_calls

    def with_call(self, call: ToolCall, *, failed: bool = False) -> "Transcript":
        """Record that a tool call was executed, and how it went.

        A repeat is counted and its fingerprint is *not* added twice, so the
        ledger stays a set of distinct requests while the counter stays a count
        of events.
        """
        repeat = self.is_repeat(call)
        return replace(
            self,
            seen_calls=self.seen_calls if repeat else (*self.seen_calls, fingerprint(call)),
            tool_calls=self.tool_calls + 1,
            tool_errors=self.tool_errors + (1 if failed else 0),
            repeated_calls=self.repeated_calls + (1 if repeat else 0),
        )

    def repeating(self) -> bool:
        """Whether repetition has gone past being worth another reminder."""
        return self.repeated_calls > self.TOLERATED_REPEATS

    def ended(self, reason: StopReason) -> "Transcript":
        """Fix the reason this loop stopped.

        The first reason wins. A loop that hits its turn limit on the same pass
        that a tool fails has stopped for whichever happened first, and
        overwriting would report the tidier of the two.
        """
        return self if self.stopped else replace(self, stop=reason)
