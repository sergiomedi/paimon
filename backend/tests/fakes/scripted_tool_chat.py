"""A tool-calling model that follows a script, turn by turn.

``FakeToolCallingChatModel`` answers the same thing every time, which is all a
single-shot caller needs. A loop needs a model that says something different on
turn two — that is the whole behaviour under test — so this one is handed a
sequence and works through it.

It also records every conversation it was shown. That is not incidental: the
messages an agent builds are where the security properties live, and "the
document's text never entered a system message" is a claim about the request,
not about the answer.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field

from paimon.domain.errors import GenerationError
from paimon.domain.ports import (
    Completion,
    Message,
    ToolCall,
    ToolCompletion,
    ToolDefinition,
)


@dataclass(frozen=True, slots=True)
class Turn:
    """One scripted model response.

    Attributes:
        text: What the model says. With no calls, this is its final answer.
        calls: What it asks to run. Empty means it is done.
        input_tokens: What the turn is to report having cost.
        output_tokens: Likewise.
    """

    text: str = ""
    calls: tuple[ToolCall, ...] = ()
    input_tokens: int = 100
    output_tokens: int = 20


def search(query: str, *, call_id: str = "call-1", **extra: object) -> ToolCall:
    """A request to search the corpus."""
    return ToolCall(call_id=call_id, name="search_corpus", arguments={"query": query, **extra})


def read(document_id: str, *, call_id: str = "call-1") -> ToolCall:
    """A request to read a document."""
    return ToolCall(call_id=call_id, name="read_document", arguments={"document_id": document_id})


@dataclass
class ScriptedToolCallingChatModel:
    """Works through a script of turns, recording what it was shown.

    Attributes:
        turns: The responses, in order. Running out raises rather than
            repeating: a test whose agent took an unexpected extra turn should
            say so, not quietly reuse an answer.
        model_id: Reported as the underlying model.
    """

    turns: Sequence[Turn] = ()
    model_id: str = "scripted-tools-v1"
    seen: list[list[Message]] = field(default_factory=list)
    """Every conversation this model was shown, oldest call first."""

    offered: list[Sequence[ToolDefinition]] = field(default_factory=list)
    """What it was offered on each call."""

    _spent: int = 0

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        temperature: float = 0.0,
        max_output_tokens: int | None = None,
    ) -> Completion:
        """Answer without tools, so this also satisfies ChatModel."""
        turn = self._next()
        return Completion(
            text=turn.text,
            model_id=self.model_id,
            input_tokens=turn.input_tokens,
            output_tokens=turn.output_tokens,
        )

    async def complete_with_tools(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
        *,
        temperature: float = 0.0,
        max_output_tokens: int | None = None,
    ) -> ToolCompletion:
        """Return the next scripted turn, recording the conversation."""
        self.seen.append(list(messages))
        self.offered.append(list(tools))
        turn = self._next()
        return ToolCompletion(
            text=turn.text,
            tool_calls=turn.calls,
            model_id=self.model_id,
            input_tokens=turn.input_tokens,
            output_tokens=turn.output_tokens,
        )

    @property
    def calls_made(self) -> int:
        """How many times the model was asked for a turn."""
        return self._spent

    def _next(self) -> Turn:
        if self._spent < len(self.turns):
            turn = self.turns[self._spent]
            self._spent += 1
            return turn
        msg = (
            f"the script ran out after {self._spent} turn(s); the agent asked for "
            "one more than the test expected"
        )
        raise GenerationError(msg)


class UnreachableToolCallingChatModel:
    """A tool-calling model whose provider is down."""

    model_id = "unreachable-tools"

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        temperature: float = 0.0,
        max_output_tokens: int | None = None,
    ) -> Completion:
        """Fail the way an unreachable provider fails."""
        msg = "chat provider unreachable: All connection attempts failed"
        raise GenerationError(msg)

    async def complete_with_tools(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
        *,
        temperature: float = 0.0,
        max_output_tokens: int | None = None,
    ) -> ToolCompletion:
        """Fail the way an unreachable provider fails."""
        msg = "chat provider unreachable: All connection attempts failed"
        raise GenerationError(msg)
