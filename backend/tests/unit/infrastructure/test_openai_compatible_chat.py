"""Tests for the OpenAI-compatible chat adapter."""

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from paimon.domain.errors import GenerationError
from paimon.domain.ports import Message
from paimon.infrastructure.chat import OpenAICompatibleChatConfig, OpenAICompatibleChatModel

Handler = Callable[[httpx.Request], httpx.Response]
CONVERSATION = [
    Message(role="system", content="Answer only from the sources."),
    Message(role="user", content="What restarts the worker?"),
]


def build(handler: Handler, **overrides: Any) -> tuple[OpenAICompatibleChatModel, list[Any]]:
    captured: list[Any] = []

    def intercept(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return handler(request)

    config = OpenAICompatibleChatConfig(
        base_url="http://model.test/v1", model="test-chat", **overrides
    )
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(intercept), base_url="http://model.test/v1"
    )
    return OpenAICompatibleChatModel(config, client=client), captured


def reply(text: str = "An answer [1].", usage: dict[str, int] | None = None) -> httpx.Response:
    body: dict[str, Any] = {
        "model": "test-chat",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}}],
    }
    if usage is not None:
        body["usage"] = usage
    return httpx.Response(200, json=body)


class TestRequests:
    async def test_messages_and_roles_are_sent(self) -> None:
        model, captured = build(lambda _r: reply())
        await model.complete(CONVERSATION)

        assert [message["role"] for message in captured[0]["messages"]] == ["system", "user"]

    async def test_temperature_defaults_to_zero(self) -> None:
        """A grounded answer should be the same answer for the same sources."""
        model, captured = build(lambda _r: reply())
        await model.complete(CONVERSATION)

        assert captured[0]["temperature"] == 0.0

    async def test_a_call_can_override_the_configured_limits(self) -> None:
        model, captured = build(lambda _r: reply(), temperature=0.7, max_output_tokens=100)
        await model.complete(CONVERSATION, temperature=0.1, max_output_tokens=32)

        assert captured[0]["temperature"] == 0.1
        assert captured[0]["max_tokens"] == 32


class TestResponses:
    async def test_the_answer_and_usage_come_back(self) -> None:
        model, _ = build(
            lambda _r: reply("Cordon it [1].", {"prompt_tokens": 120, "completion_tokens": 8})
        )
        completion = await model.complete(CONVERSATION)

        assert completion.text == "Cordon it [1]."
        assert completion.input_tokens == 120
        assert completion.output_tokens == 8
        assert completion.total_tokens == 128

    async def test_missing_usage_becomes_zeros_rather_than_an_error(self) -> None:
        """Losing cost attribution for one request is worse than losing the
        request, and the zeros are visible in the traces that consume them."""
        model, _ = build(lambda _r: reply("An answer."))
        completion = await model.complete(CONVERSATION)

        assert completion.text == "An answer."
        assert completion.total_tokens == 0


class TestFailures:
    async def test_an_http_error_becomes_a_generation_error(self) -> None:
        model, _ = build(lambda _r: httpx.Response(500, json={"error": "boom"}))
        with pytest.raises(GenerationError, match="returned 500"):
            await model.complete(CONVERSATION)

    async def test_an_unreachable_provider_becomes_a_generation_error(self) -> None:
        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        model, _ = build(refuse)
        with pytest.raises(GenerationError, match="unreachable"):
            await model.complete(CONVERSATION)

    async def test_a_body_without_choices_is_refused(self) -> None:
        model, _ = build(lambda _r: httpx.Response(200, json={"model": "test-chat"}))
        with pytest.raises(GenerationError, match="no message content"):
            await model.complete(CONVERSATION)

    async def test_an_empty_answer_is_refused(self) -> None:
        """An empty answer would be reported as ungrounded and indistinguishable
        from a considered refusal."""
        model, _ = build(lambda _r: reply("   "))
        with pytest.raises(GenerationError, match="empty answer"):
            await model.complete(CONVERSATION)
