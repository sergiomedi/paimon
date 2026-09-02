"""A scriptable in-memory ChatModel."""

from collections.abc import Sequence

from paimon.domain.ports import Completion, Message


class FakeChatModel:
    """Returns a canned answer and reports plausible token usage."""

    def __init__(self, answer: str = "an answer", model_id: str = "fake-chat-v1") -> None:
        self._answer = answer
        self._model_id = model_id
        self.calls: list[Sequence[Message]] = []

    @property
    def model_id(self) -> str:
        return self._model_id

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        temperature: float = 0.0,
        max_output_tokens: int | None = None,
    ) -> Completion:
        self.calls.append(list(messages))
        input_tokens = sum(len(message.content.split()) for message in messages)
        answer = self._answer
        if max_output_tokens is not None:
            answer = " ".join(answer.split()[:max_output_tokens])
        return Completion(
            text=answer,
            model_id=self._model_id,
            input_tokens=max(input_tokens, 1),
            output_tokens=max(len(answer.split()), 1),
        )
