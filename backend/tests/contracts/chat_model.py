"""Contract for the ChatModel port."""

import pytest

from paimon.domain.ports import ChatModel, Message


class ChatModelContract:
    """Every ChatModel adapter must pass these."""

    @pytest.fixture
    def chat_model(self) -> ChatModel:
        """Supplied by the subclass."""
        raise NotImplementedError

    @staticmethod
    def _conversation() -> list[Message]:
        return [
            Message(role="system", content="Answer only from the provided context."),
            Message(role="user", content="What restarts the ingest worker?"),
        ]

    async def test_it_answers(self, chat_model: ChatModel) -> None:
        completion = await chat_model.complete(self._conversation())
        assert completion.text.strip()

    async def test_it_reports_which_model_answered(self, chat_model: ChatModel) -> None:
        """Traces and cost reports are attributed by this; an answer that cannot
        say which model produced it cannot be audited later."""
        completion = await chat_model.complete(self._conversation())
        assert completion.model_id == chat_model.model_id

    async def test_it_reports_token_usage(self, chat_model: ChatModel) -> None:
        """Per-request cost attribution is a Phase 5 deliverable, and a provider
        that does not report usage cannot be made to report it afterwards."""
        completion = await chat_model.complete(self._conversation())
        assert completion.input_tokens > 0
        assert completion.output_tokens > 0
        assert completion.total_tokens == completion.input_tokens + completion.output_tokens
