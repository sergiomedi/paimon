"""Translating between this platform's tool vocabulary and the OpenAI wire shape.

Shared by the local and Azure adapters, which speak the same protocol here. One
translation rather than two: the parsing has enough judgement in it — what a
malformed argument object means, what an empty response means — that two copies
would be two answers to the same question.
"""

import json
from collections.abc import Mapping, Sequence
from typing import Any

from paimon.domain.errors import GenerationError
from paimon.domain.ports import Message, ToolCall, ToolCompletion, ToolDefinition


def encode_tools(tools: Sequence[ToolDefinition]) -> list[dict[str, Any]]:
    """Render tool declarations in the shape the API expects."""
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": dict(tool.parameters),
            },
        }
        for tool in tools
    ]


def encode_message(message: Message) -> dict[str, Any]:
    """Render one turn, including any tool calls or tool results it carries."""
    encoded: dict[str, Any] = {"role": message.role, "content": message.content}
    if message.tool_call_id is not None:
        encoded["tool_call_id"] = message.tool_call_id
    if message.tool_calls:
        encoded["tool_calls"] = [
            {
                "id": call.call_id,
                "type": "function",
                "function": {"name": call.name, "arguments": json.dumps(dict(call.arguments))},
            }
            for call in message.tool_calls
        ]
    return encoded


def _arguments(raw: Any, name: str) -> Mapping[str, Any]:
    """Parse a tool call's arguments, which arrive as a JSON string.

    A model that produces invalid JSON here has not asked for anything the
    platform can act on, so this is an error rather than an empty argument set.
    Running a tool with no arguments because its arguments could not be read is
    how a search for nothing gets reported as a search that found nothing.
    """
    if raw in (None, ""):
        return {}
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError as error:
        msg = f"model requested tool '{name}' with arguments that are not valid JSON"
        raise GenerationError(msg) from error
    if not isinstance(parsed, dict):
        msg = f"model requested tool '{name}' with arguments that are not an object"
        raise GenerationError(msg)
    return parsed


def parse_tool_completion(body: Any, fallback_model: str) -> ToolCompletion:
    """Read a chat completion that may contain tool calls.

    Raises:
        GenerationError: If the response carries neither text nor a tool call.
    """
    try:
        message = body["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as error:
        msg = "chat response contains no message"
        raise GenerationError(msg) from error

    text = message.get("content") or ""
    raw_calls = message.get("tool_calls") or []
    calls: list[ToolCall] = []
    for raw in raw_calls:
        function = raw.get("function") or {}
        name = str(function.get("name") or "")
        if not name:
            msg = "model requested a tool without naming it"
            raise GenerationError(msg)
        calls.append(
            ToolCall(
                call_id=str(raw.get("id") or name),
                name=name,
                arguments=_arguments(function.get("arguments"), name),
            )
        )

    if not text.strip() and not calls:
        # Neither an answer nor a request. Reported rather than returned as an
        # empty answer, because a caller cannot tell the two apart and would
        # present silence as a conclusion.
        msg = "chat provider returned neither text nor a tool call"
        raise GenerationError(msg)

    usage = body.get("usage") or {}
    return ToolCompletion(
        text=text,
        tool_calls=tuple(calls),
        model_id=str(body.get("model") or fallback_model),
        input_tokens=int(usage.get("prompt_tokens") or 0),
        output_tokens=int(usage.get("completion_tokens") or 0),
    )
