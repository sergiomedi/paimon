"""Adapters implementing the ChatModel port."""

from paimon.infrastructure.chat.openai_compatible import (
    OpenAICompatibleChatConfig,
    OpenAICompatibleChatModel,
)

__all__ = ["OpenAICompatibleChatConfig", "OpenAICompatibleChatModel"]
