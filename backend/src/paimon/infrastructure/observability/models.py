"""Tracing for model calls, added by wrapping rather than by editing.

There are two chat adapters and two embedding adapters, and there will be more.
Instrumentation written inside each is the same code repeated, with as many
chances to drift as there are copies — and it would put a tracing import into
every adapter, so a new one is only traced if its author remembers.

A decorator over the port is traced by construction. The composition root wraps
whatever it built; a fake in a test is wrapped by the same code as a real
adapter; and the span covers the whole logical call, so the retries the httpx
instrumentation records appear as children rather than as separate operations.

**The one hazard, and it is a real one.** ``ToolCallingChatModel`` is a
capability this platform tests for with ``isinstance``. A decorator that
implemented only ``ChatModel`` would silently *remove* that capability from an
adapter that had it, and the symptom would not be an error — it would be agents
quietly never being offered tools. :func:`trace_chat_model` picks its wrapper by
what it is given, and a test asserts the capability survives.
"""

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from opentelemetry.trace import Span

from paimon.domain.ports import (
    ChatModel,
    Completion,
    EmbeddingModel,
    Message,
    ToolCallingChatModel,
    ToolCompletion,
    ToolDefinition,
)
from paimon.domain.value_objects import Embedding
from paimon.observability.genai import (
    EMBEDDINGS_RETURNED,
    INPUT_COUNT,
    INPUT_MESSAGES,
    OUTPUT_MESSAGES,
    RESPONSE_MODEL,
    TOOL_CALLS,
    TOOL_COUNT,
    Operation,
    Provider,
    model_span,
    record_usage,
)


@runtime_checkable
class ToolCallingChat(ChatModel, ToolCallingChatModel, Protocol):
    """Both halves of a tool-calling model, as one type.

    ``ToolCallingChatModel`` declares only the tool method — it is a capability
    added to a chat model, not a replacement for one — so neither protocol alone
    describes what this wrapper holds. Naming the conjunction is also what lets
    the type checker agree that wrapping one is safe.
    """


class TracedChatModel:
    """A chat model that records a ``gen_ai`` span per call."""

    def __init__(
        self, inner: ChatModel, provider: Provider, *, capture_content: bool = False
    ) -> None:
        """Wrap a chat model.

        Args:
            inner: The adapter doing the work.
            provider: Which provider it talks to, as the registry spells it.
            capture_content: Record prompts and completions on the span. Off by
                default and refused in deployed environments — the conventions
                mark content opt-in because it is likely to carry sensitive data,
                and here that is an organization's documentation (ADR-0025).
        """
        self._inner = inner
        self._provider = provider
        self._capture_content = capture_content

    @property
    def model_id(self) -> str:
        """Identifier of the underlying model."""
        return self._inner.model_id

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        temperature: float = 0.0,
        max_output_tokens: int | None = None,
    ) -> Completion:
        """Generate an answer, recording the call."""
        with model_span(
            Operation.CHAT,
            self._provider,
            self._inner.model_id,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
        ) as span:
            if self._capture_content:
                span.set_attribute(INPUT_MESSAGES, _render(messages))
            completion = await self._inner.complete(
                messages, temperature=temperature, max_output_tokens=max_output_tokens
            )
            record_usage(
                span,
                model=completion.model_id,
                input_tokens=completion.input_tokens,
                output_tokens=completion.output_tokens,
            )
            if self._capture_content:
                span.set_attribute(OUTPUT_MESSAGES, completion.text)
            return completion


class TracedToolCallingChatModel(TracedChatModel):
    """The same, for a model that can also be offered tools.

    Subclassed rather than composed so that one wrapper satisfies both protocols
    at once, which is what a caller holding a ``ChatModel`` and a caller checking
    for the capability each need it to be.
    """

    def __init__(
        self, inner: ToolCallingChat, provider: Provider, *, capture_content: bool = False
    ) -> None:
        """Wrap a tool-calling chat model."""
        super().__init__(inner, provider, capture_content=capture_content)
        self._tool_calling = inner

    async def complete_with_tools(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
        *,
        temperature: float = 0.0,
        max_output_tokens: int | None = None,
    ) -> ToolCompletion:
        """Generate with tools offered, recording the call."""
        with model_span(
            Operation.CHAT,
            self._provider,
            self.model_id,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
        ) as span:
            # How many definitions were sent, because they are sent on every turn
            # and are therefore input tokens paid again each time. A conversation
            # whose cost climbs for no visible reason usually has this at the
            # bottom of it, and nothing else on the span would show it.
            span.set_attribute(TOOL_COUNT, len(tools))
            if self._capture_content:
                span.set_attribute(INPUT_MESSAGES, _render(messages))
            completion = await self._tool_calling.complete_with_tools(
                messages, tools, temperature=temperature, max_output_tokens=max_output_tokens
            )
            record_usage(
                span,
                model=completion.model_id,
                input_tokens=completion.input_tokens,
                output_tokens=completion.output_tokens,
            )
            # Names only. Which tools a model asked for is the shape of its
            # reasoning; the arguments are the caller's data and are not.
            span.set_attribute(TOOL_CALLS, [call.name for call in completion.tool_calls])
            if self._capture_content:
                span.set_attribute(OUTPUT_MESSAGES, completion.text)
            return completion


class TracedEmbeddingModel:
    """An embedding model that records a ``gen_ai`` span per call."""

    def __init__(self, inner: EmbeddingModel, provider: Provider) -> None:
        """Wrap an embedding model.

        No ``capture_content``: what is embedded during ingestion is the corpus
        itself, so recording it would copy an organization's documentation into
        a tracing backend one chunk at a time.
        """
        self._inner = inner
        self._provider = provider

    @property
    def model_id(self) -> str:
        """Identifier written onto every embedding this model produces."""
        return self._inner.model_id

    @property
    def dimensions(self) -> int:
        """Dimensionality of the vectors this model produces."""
        return self._inner.dimensions

    async def embed_documents(self, texts: Sequence[str]) -> list[Embedding]:
        """Embed text destined for the index, recording the call."""
        with model_span(Operation.EMBEDDINGS, self._provider, self._inner.model_id) as span:
            # The count, not the text: what is embedded during ingestion is
            # the corpus, and recording it would copy an organization's
            # documentation into a tracing backend one chunk at a time.
            span.set_attribute(INPUT_COUNT, len(texts))
            embeddings = await self._inner.embed_documents(texts)
            _record_embedding_usage(span, self._inner.model_id, len(embeddings))
            return embeddings

    async def embed_query(self, text: str) -> Embedding:
        """Embed a search query, recording the call."""
        with model_span(Operation.EMBEDDINGS, self._provider, self._inner.model_id) as span:
            span.set_attribute(INPUT_COUNT, 1)
            embedding = await self._inner.embed_query(text)
            _record_embedding_usage(span, self._inner.model_id, 1)
            return embedding


def trace_chat_model(
    inner: ChatModel, provider: Provider, *, capture_content: bool = False
) -> ChatModel:
    """Wrap a chat model without losing what it can do.

    Args:
        inner: The adapter to wrap.
        provider: Which provider it talks to.
        capture_content: Record prompts and completions on spans.

    Returns:
        A wrapper that also satisfies :class:`ToolCallingChatModel` when the
        wrapped adapter does. Getting this wrong would not raise: agents would
        simply stop being offered tools, everywhere, silently.
    """
    if isinstance(inner, ToolCallingChat):
        return TracedToolCallingChatModel(inner, provider, capture_content=capture_content)
    return TracedChatModel(inner, provider, capture_content=capture_content)


def trace_embedding_model(inner: EmbeddingModel, provider: Provider) -> EmbeddingModel:
    """Wrap an embedding model."""
    return TracedEmbeddingModel(inner, provider)


def _record_embedding_usage(span: Span, model: str, count: int) -> None:
    """Record what an embedding call produced.

    Token usage is deliberately absent. The port does not carry it — an embedding
    is a vector, not a completion — and counting tokens here with a tokenizer that
    is not the provider's would put a number on a span that reads as measured and
    is estimated. Batch 4 adds usage where a provider reports it.
    """
    if not span.is_recording():
        return
    span.set_attribute(RESPONSE_MODEL, model)
    span.set_attribute(EMBEDDINGS_RETURNED, count)


def _render(messages: Sequence[Message]) -> str:
    """Render a conversation for a span attribute."""
    return "\n\n".join(f"{message.role}: {message.content}" for message in messages)


__all__ = [
    "TracedChatModel",
    "TracedEmbeddingModel",
    "TracedToolCallingChatModel",
    "trace_chat_model",
    "trace_embedding_model",
]
