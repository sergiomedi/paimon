"""Adapters that add observability to other adapters.

Decorators over the ports rather than instrumentation inside each adapter. The
reasons are in ADR-0026; the short one is that there are two chat adapters, two
embedding adapters and two vector stores, and tracing written into each is the
same code six times with six chances to drift.
"""

from paimon.infrastructure.observability.models import (
    TracedChatModel,
    TracedEmbeddingModel,
    TracedToolCallingChatModel,
    trace_chat_model,
    trace_embedding_model,
)

__all__ = [
    "TracedChatModel",
    "TracedEmbeddingModel",
    "TracedToolCallingChatModel",
    "trace_chat_model",
    "trace_embedding_model",
]
