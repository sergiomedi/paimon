"""Port for text generation."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

Role = Literal["system", "user", "assistant", "tool"]


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """A tool a model may ask the platform to run.

    ``parameters`` is a JSON Schema object. Declared as data rather than derived
    from a Python signature: the same declaration has to reach a model provider,
    an MCP client in Phase 4 and this platform's own executor, and a schema all
    three read is one definition rather than three that drift.
    """

    name: str
    description: str
    parameters: Mapping[str, Any]

    def __post_init__(self) -> None:
        """Reject a tool a model could not choose sensibly.

        Raises:
            ValueError: If the tool has no name, or no description. A model picks
                a tool by reading its description; an undescribed tool is either
                never chosen or chosen at random, and both are worse than absent.
        """
        if not self.name.strip():
            msg = "a tool must be named"
            raise ValueError(msg)
        if not self.description.strip():
            msg = f"tool '{self.name}' needs a description: it is how a model chooses it"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A model's request to run one tool.

    ``arguments`` is what the model produced, already parsed. Providers return it
    as a JSON string, and parsing at the adapter boundary means one place decides
    what a malformed argument object means rather than every caller.
    """

    call_id: str
    name: str
    arguments: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class Message:
    """One turn of a conversation.

    ``tool_calls`` belongs to an assistant turn that asked for tools;
    ``tool_call_id`` to a ``tool`` turn carrying one result. Both are optional so
    that a conversation without tools is exactly what it was before.
    """

    role: Role
    content: str
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None


@dataclass(frozen=True, slots=True)
class Completion:
    """A model's answer, with what it cost to produce.

    Token counts are part of the contract rather than an optional extra: cost
    attribution per request is a Phase 5 deliverable, and a provider that does not
    report usage cannot be made to report it after the fact.
    """

    text: str
    model_id: str
    input_tokens: int
    output_tokens: int

    @property
    def total_tokens(self) -> int:
        """Tokens consumed by the request and its answer."""
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True, slots=True)
class ToolCompletion:
    """What a model returned when it was offered tools.

    Both fields can be populated: a model may explain itself and ask for a tool
    in the same turn. Both can also be empty, which is a provider that answered
    nothing and is reported as an error rather than as an empty answer.
    """

    text: str
    tool_calls: tuple[ToolCall, ...]
    model_id: str
    input_tokens: int
    output_tokens: int

    @property
    def total_tokens(self) -> int:
        """Tokens consumed by the request and its answer."""
        return self.input_tokens + self.output_tokens

    @property
    def wants_tools(self) -> bool:
        """Whether the model asked for anything to be run."""
        return bool(self.tool_calls)


@runtime_checkable
class ChatModel(Protocol):
    """Generates text from a sequence of messages."""

    @property
    def model_id(self) -> str:
        """Identifier of the underlying model."""
        ...

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        temperature: float = 0.0,
        max_output_tokens: int | None = None,
    ) -> Completion:
        """Generate an answer.

        Args:
            messages: Conversation so far, oldest first.
            temperature: Sampling temperature. Defaults to zero because grounded
                answering wants reproducibility, not variety.
            max_output_tokens: Upper bound on the answer length, if any.

        Returns:
            The generated answer and its token usage.

        Raises:
            GenerationError: If the provider could not produce an answer.
        """
        ...


@runtime_checkable
class ToolCallingChatModel(Protocol):
    """A chat model that can be offered tools and ask for them.

    A capability, kept off :class:`ChatModel` for the reason ADR-0014 gave for
    :class:`~paimon.domain.ports.NativeHybridSearch` and ADR-0016 restated for
    agents: not every model does this reliably, and several of the local models
    this platform is meant to run on do it badly enough to be worse than not at
    all. A boolean on the port would make every caller remember to check; a
    protocol lets the type checker ask.

    Implementations also satisfy :class:`ChatModel`, so a caller that does not
    care about tools never has to know which one it holds.
    """

    async def complete_with_tools(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
        *,
        temperature: float = 0.0,
        max_output_tokens: int | None = None,
    ) -> ToolCompletion:
        """Generate, offering the model a set of tools it may ask for.

        Args:
            messages: Conversation so far, oldest first.
            tools: What the model may request. An empty sequence means the model
                is being asked to answer from what it already has.
            temperature: Sampling temperature.
            max_output_tokens: Upper bound on the answer length, if any.

        Returns:
            The text produced, the tools requested, and what it cost.

        Raises:
            GenerationError: If the provider could not produce a response, or
                returned a tool call this platform cannot act on.
        """
        ...
