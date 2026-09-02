"""Adapters implementing the EmbeddingModel port."""

from paimon.infrastructure.embedding.openai_compatible import (
    OpenAICompatibleConfig,
    OpenAICompatibleEmbeddingModel,
)

__all__ = ["OpenAICompatibleConfig", "OpenAICompatibleEmbeddingModel"]
