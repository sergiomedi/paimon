"""Contract for the ToolCallingChatModel capability.

Behaviour, not judgement. These fix what a model's tool calls must look like by
the time they reach the platform — named, addressable, with parsed arguments —
and say nothing about whether the model chose well. Which tool a model picks is
a question for the Phase 6 benchmark, and asserting it here would make every
adapter's contract depend on a model's mood.
"""

import pytest

from paimon.domain.errors import GenerationError
from paimon.domain.ports import Message, ToolCallingChatModel, ToolDefinition

SEARCH = ToolDefinition(
    name="search_corpus",
    description="Search the indexed corpus for passages about a topic.",
    parameters={
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    },
)

ASK = (Message(role="user", content="what does the runbook say about draining?"),)


class ToolCallingChatModelContract:
    """Every ToolCallingChatModel adapter must pass these."""

    @pytest.fixture
    def tool_model(self) -> ToolCallingChatModel:
        """Supplied by the subclass, scripted to request a tool."""
        raise NotImplementedError

    @pytest.fixture
    def text_only_model(self) -> ToolCallingChatModel:
        """Supplied by the subclass, scripted to answer without tools."""
        raise NotImplementedError

    @pytest.fixture
    def empty_model(self) -> ToolCallingChatModel:
        """Supplied by the subclass, scripted to return neither text nor a call."""
        raise NotImplementedError

    async def test_it_satisfies_the_capability(self, tool_model: ToolCallingChatModel) -> None:
        assert isinstance(tool_model, ToolCallingChatModel)

    async def test_a_requested_tool_comes_back_named(
        self, tool_model: ToolCallingChatModel
    ) -> None:
        completion = await tool_model.complete_with_tools(ASK, [SEARCH])
        assert completion.wants_tools
        assert completion.tool_calls[0].name == SEARCH.name

    async def test_arguments_arrive_parsed_not_as_a_string(
        self, tool_model: ToolCallingChatModel
    ) -> None:
        # Providers send them as a JSON string. Parsing at the boundary means one
        # place decides what a malformed argument object means.
        completion = await tool_model.complete_with_tools(ASK, [SEARCH])
        assert completion.tool_calls[0].arguments["query"]

    async def test_a_call_is_addressable_so_its_result_can_be_returned(
        self, tool_model: ToolCallingChatModel
    ) -> None:
        completion = await tool_model.complete_with_tools(ASK, [SEARCH])
        assert completion.tool_calls[0].call_id

    async def test_an_answer_without_tools_is_a_normal_outcome(
        self, text_only_model: ToolCallingChatModel
    ) -> None:
        completion = await text_only_model.complete_with_tools(ASK, [SEARCH])
        assert not completion.wants_tools
        assert completion.text

    async def test_usage_is_reported(self, tool_model: ToolCallingChatModel) -> None:
        completion = await tool_model.complete_with_tools(ASK, [SEARCH])
        assert completion.total_tokens >= 0

    async def test_neither_text_nor_a_call_is_an_error(
        self, empty_model: ToolCallingChatModel
    ) -> None:
        # A caller cannot tell silence from a conclusion, and would present the
        # first as the second.
        with pytest.raises(GenerationError, match="neither text nor a tool call"):
            await empty_model.complete_with_tools(ASK, [SEARCH])

    async def test_offering_no_tools_still_answers(
        self, text_only_model: ToolCallingChatModel
    ) -> None:
        completion = await text_only_model.complete_with_tools(ASK, [])
        assert completion.text
