"""A scriptable ToolCallingChatModel.

Returns whatever it was built with, so a test can pin the three outcomes that
matter — a tool request, a plain answer, and silence — without a model server.
"""

from collections.abc import Sequence

from paimon.domain.errors import GenerationError
from paimon.domain.ports import (
    Completion,
    Message,
    ToolCall,
    ToolCompletion,
    ToolDefinition,
)


class FakeToolCallingChatModel:
    """Answers with a scripted set of tool calls, or with text."""

    def __init__(
        self,
        *,
        text: str = "",
        tool_calls: Sequence[ToolCall] = (),
        model_id: str = "fake-tools-v1",
    ) -> None:
        """Script the model's single response."""
        self._text = text
        self._tool_calls = tuple(tool_calls)
        self._model_id = model_id
        self.offered: list[Sequence[ToolDefinition]] = []

    @property
    def model_id(self) -> str:
        """Identifier of the underlying model."""
        return self._model_id

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        temperature: float = 0.0,
        max_output_tokens: int | None = None,
    ) -> Completion:
        """Answer without tools, so the fake also satisfies ChatModel."""
        if not self._text:
            msg = "chat provider returned an empty answer"
            raise GenerationError(msg)
        return Completion(
            text=self._text,
            model_id=self._model_id,
            input_tokens=len(messages),
            output_tokens=max(len(self._text.split()), 1),
        )

    async def complete_with_tools(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
        *,
        temperature: float = 0.0,
        max_output_tokens: int | None = None,
    ) -> ToolCompletion:
        """Return the scripted response, recording what it was offered."""
        self.offered.append(list(tools))
        if not self._text.strip() and not self._tool_calls:
            msg = "chat provider returned neither text nor a tool call"
            raise GenerationError(msg)
        return ToolCompletion(
            text=self._text,
            tool_calls=self._tool_calls,
            model_id=self._model_id,
            input_tokens=len(messages),
            output_tokens=max(len(self._text.split()), 1),
        )
