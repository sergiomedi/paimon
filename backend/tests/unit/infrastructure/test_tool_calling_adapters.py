"""Both chat adapters, run against the same tool-calling contract.

The point of the exercise: the two backends speak the same wire protocol here,
so the same twelve assertions run against both, and a difference between the
local endpoint and Azure shows up as a failing test rather than as an agent that
behaves differently in production.
"""

from typing import Any

import httpx
import pytest

from paimon.domain.errors import GenerationError
from paimon.domain.ports import ToolCallingChatModel
from paimon.infrastructure.azure.credentials import ApiKeyCredential
from paimon.infrastructure.azure.openai import AzureOpenAIChatModel, AzureOpenAIConfig
from paimon.infrastructure.chat import OpenAICompatibleChatConfig, OpenAICompatibleChatModel
from tests.contracts.tool_calling import ToolCallingChatModelContract

TOOL_CALL_BODY: dict[str, Any] = {
    "model": "test-chat",
    "choices": [
        {
            "index": 0,
            "finish_reason": "tool_calls",
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_abc123",
                        "type": "function",
                        "function": {
                            "name": "search_corpus",
                            # A JSON string, which is how providers send it.
                            "arguments": '{"query": "draining"}',
                        },
                    }
                ],
            },
        }
    ],
    "usage": {"prompt_tokens": 80, "completion_tokens": 12},
}

TEXT_BODY: dict[str, Any] = {
    "model": "test-chat",
    "choices": [
        {"index": 0, "message": {"role": "assistant", "content": "Cordon the node first."}}
    ],
    "usage": {"prompt_tokens": 40, "completion_tokens": 6},
}

EMPTY_BODY: dict[str, Any] = {
    "model": "test-chat",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": ""}}],
}


def responder(body: dict[str, Any]) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    return httpx.MockTransport(handle)


def local(body: dict[str, Any]) -> OpenAICompatibleChatModel:
    return OpenAICompatibleChatModel(
        OpenAICompatibleChatConfig(base_url="http://model.test/v1", model="test-chat"),
        client=httpx.AsyncClient(transport=responder(body), base_url="http://model.test/v1"),
    )


def azure(body: dict[str, Any]) -> AzureOpenAIChatModel:
    return AzureOpenAIChatModel(
        AzureOpenAIConfig(endpoint="https://resource.test", deployment="test-chat"),
        ApiKeyCredential("k"),
        client=httpx.AsyncClient(transport=responder(body), base_url="https://resource.test"),
    )


class TestOpenAICompatibleToolCalling(ToolCallingChatModelContract):
    @pytest.fixture
    def tool_model(self) -> ToolCallingChatModel:
        return local(TOOL_CALL_BODY)

    @pytest.fixture
    def text_only_model(self) -> ToolCallingChatModel:
        return local(TEXT_BODY)

    @pytest.fixture
    def empty_model(self) -> ToolCallingChatModel:
        return local(EMPTY_BODY)


class TestAzureOpenAIToolCalling(ToolCallingChatModelContract):
    @pytest.fixture
    def tool_model(self) -> ToolCallingChatModel:
        return azure(TOOL_CALL_BODY)

    @pytest.fixture
    def text_only_model(self) -> ToolCallingChatModel:
        return azure(TEXT_BODY)

    @pytest.fixture
    def empty_model(self) -> ToolCallingChatModel:
        return azure(EMPTY_BODY)


class TestArgumentsThatCannotBeActedOn:
    """A model can ask for something the platform cannot run."""

    async def test_arguments_that_are_not_valid_json_are_refused(self) -> None:
        # Running the tool with no arguments instead is how a search for nothing
        # gets reported as a search that found nothing.
        body = {
            "model": "test-chat",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "c1",
                                "function": {"name": "search_corpus", "arguments": "{not json"},
                            }
                        ],
                    }
                }
            ],
        }
        with pytest.raises(GenerationError, match="not valid JSON"):
            await local(body).complete_with_tools([], [])

    async def test_an_unnamed_tool_call_is_refused(self) -> None:
        body = {
            "model": "test-chat",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{"id": "c1", "function": {"arguments": "{}"}}],
                    }
                }
            ],
        }
        with pytest.raises(GenerationError, match="without naming it"):
            await local(body).complete_with_tools([], [])
