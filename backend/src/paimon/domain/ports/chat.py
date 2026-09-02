"""Port for text generation."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

Role = Literal["system", "user", "assistant"]


@dataclass(frozen=True, slots=True)
class Message:
    """One turn of a conversation."""

    role: Role
    content: str


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
