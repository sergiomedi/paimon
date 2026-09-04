"""The GenAI semantic conventions, written down once.

Every span this platform emits for a model call is built here, so that when the
conventions move — and they will, they are still marked *Development* and have
already renamed ``gen_ai.system`` to ``gen_ai.provider.name`` and
``prompt_tokens`` to ``input_tokens`` — the platform moves in one file rather
than in every adapter.

The span name is prescribed: ``{operation} {model}``, so ``chat gpt-4o-mini``.
Not the URL, and not a constant: a backend groups by name, and a name carrying an
id would give a dashboard one row per request while a constant would give it one
row for everything.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from enum import StrEnum
from typing import Final

from opentelemetry.trace import Span, SpanKind

from paimon.observability.tracing import get_tracer, record_error

OPERATION: Final = "gen_ai.operation.name"
PROVIDER: Final = "gen_ai.provider.name"
REQUEST_MODEL: Final = "gen_ai.request.model"
RESPONSE_MODEL: Final = "gen_ai.response.model"
REQUEST_TEMPERATURE: Final = "gen_ai.request.temperature"
REQUEST_MAX_TOKENS: Final = "gen_ai.request.max_tokens"
INPUT_TOKENS: Final = "gen_ai.usage.input_tokens"
OUTPUT_TOKENS: Final = "gen_ai.usage.output_tokens"

#: Message content. Opt-in in the conventions, which warn it is likely to carry
#: sensitive data, and gated here by ``tracing.capture_content`` (ADR-0025).
INPUT_MESSAGES: Final = "gen_ai.input.messages"
OUTPUT_MESSAGES: Final = "gen_ai.output.messages"

#: Not in the conventions, and namespaced so it is obvious which are ours. How
#: many tools a model was offered explains a bill that has no other explanation:
#: definitions are sent on every turn, so they are input tokens paid repeatedly.
TOOL_COUNT: Final = "paimon.gen_ai.tool_count"
TOOL_CALLS: Final = "paimon.gen_ai.tool_calls"

#: How many texts one embedding call carried, and how many vectors came back.
#: An ingestion that is slow because it sends one text per request looks exactly
#: like one that is slow because the provider is, until this is on the span.
INPUT_COUNT: Final = "paimon.gen_ai.input_count"
EMBEDDINGS_RETURNED: Final = "paimon.gen_ai.embeddings_returned"

#: Retrieval. The data source is the index a query went to, which is what makes
#: two backends comparable in one dashboard.
DATA_SOURCE: Final = "gen_ai.data_source.id"
RETRIEVAL_TOP_K: Final = "gen_ai.retrieval.top_k"
#: Opt-In in the conventions, for the same reason message content is.
RETRIEVAL_QUERY: Final = "gen_ai.retrieval.query.text"

#: Agents and tools.
AGENT_NAME: Final = "gen_ai.agent.name"
CONVERSATION_ID: Final = "gen_ai.conversation.id"
TOOL_NAME: Final = "gen_ai.tool.name"
TOOL_CALL_ID: Final = "gen_ai.tool.call.id"
TOOL_TYPE: Final = "gen_ai.tool.type"
#: Whose material a span acted on. Not in the conventions, and the single
#: most useful thing on a span in a multi-tenant platform: a question about
#: one organization's traffic is unanswerable without it.
TENANT: Final = "paimon.tenant_id"

#: Ours. How many hits came back, which is the difference between "retrieval was
#: slow" and "retrieval found nothing", and between "the model was wrong" and
#: "the model was given nothing to be right about".
HITS_RETURNED: Final = "paimon.retrieval.hits"
#: A node of a deterministic graph. The conventions have no concept for it: they
#: describe agents that decide their own next step, and these do not.
AGENT_NODE: Final = "paimon.agent.node"
AGENT_STEP_TOKENS: Final = "paimon.agent.step_tokens"
#: Whether this span continues a run that had stopped for a person. The two
#: halves are separate traces, minutes or hours apart, joined by the thread id.
RESUMED: Final = "paimon.agent.resumed"


class Operation(StrEnum):
    """The operations this platform performs, named as the registry names them.

    A closed set rather than free strings, because these values are what a
    backend filters and charts by, and a typo produces a category that quietly
    contains one span.
    """

    CHAT = "chat"
    EMBEDDINGS = "embeddings"
    EXECUTE_TOOL = "execute_tool"
    INVOKE_AGENT = "invoke_agent"
    RETRIEVAL = "retrieval"


class Provider(StrEnum):
    """Providers this platform talks to, as the registry spells them.

    ``OPENAI`` names the **API dialect**, not the company. This platform's
    non-Azure adapter speaks the OpenAI wire format at whatever endpoint it is
    pointed at — Ollama, vLLM, a gateway — and the registry has no value for
    "something OpenAI-compatible". Naming the dialect is the honest reading of an
    attribute whose purpose is telling a reader what the call looked like.
    """

    OPENAI = "openai"
    AZURE_OPENAI = "azure.ai.openai"


@contextmanager
def model_span(
    operation: Operation,
    provider: Provider,
    model: str,
    *,
    temperature: float | None = None,
    max_output_tokens: int | None = None,
) -> Iterator[Span]:
    """Open a span for one call to a model provider.

    ``SpanKind.CLIENT`` because this platform is the caller. A backend uses the
    kind to decide what is a dependency of what, and a client span mislabelled
    ``INTERNAL`` disappears from the picture that shows where a request's time
    went.

    One span per *logical* call, not per HTTP attempt: the httpx instrumentation
    already opens a child span per attempt, so a retry shows up inside this one
    rather than beside it — which is what makes "the model took nine seconds"
    and "the model took three attempts" both visible at once.

    Args:
        operation: What is being asked of the model.
        provider: Which provider, as the registry spells it.
        model: Model or deployment the request names.
        temperature: Sampling temperature, when the caller chose one.
        max_output_tokens: Requested ceiling, when there is one.

    Yields:
        The span, so a caller can record what came back.
    """
    attributes: dict[str, str | int | float] = {
        OPERATION: operation.value,
        PROVIDER: provider.value,
        REQUEST_MODEL: model,
    }
    if temperature is not None:
        attributes[REQUEST_TEMPERATURE] = temperature
    if max_output_tokens is not None:
        attributes[REQUEST_MAX_TOKENS] = max_output_tokens

    with get_tracer().start_as_current_span(
        f"{operation.value} {model}", kind=SpanKind.CLIENT, attributes=attributes
    ) as span:
        try:
            yield span
        except Exception as error:
            record_error(span, error)
            raise


@contextmanager
def operation_span(
    operation: Operation,
    target: str,
    *,
    kind: SpanKind = SpanKind.INTERNAL,
    attributes: dict[str, str | int | float] | None = None,
) -> Iterator[Span]:
    """Open a span for an operation that is not a call to a model provider.

    Retrieval, a tool execution, an agent run. Named the same way — the operation
    followed by what it acted on — because a reader scanning a trace should not
    have to learn two naming schemes to read one request.

    Args:
        operation: What is happening.
        target: The index, tool or agent it happens to.
        kind: ``CLIENT`` when this platform is calling something outside itself,
            ``INTERNAL`` when the work happens here.
        attributes: Set on the span at creation, so a sampler can see them.

    Yields:
        The span.
    """
    with get_tracer().start_as_current_span(
        f"{operation.value} {target}".strip(),
        kind=kind,
        attributes={OPERATION: operation.value, **(attributes or {})},
    ) as span:
        try:
            yield span
        except Exception as error:
            record_error(span, error)
            raise


def record_usage(span: Span, *, model: str, input_tokens: int, output_tokens: int) -> None:
    """Record what a call returned and what it cost.

    The response model is recorded separately from the requested one because they
    differ in ways that matter: an alias resolves to a dated build, and a
    deployment name is not a model name at all. A regression that arrives without
    a deployment is a provider changing what the alias points at, and only the
    response model shows it.
    """
    if not span.is_recording():
        return
    span.set_attribute(RESPONSE_MODEL, model)
    span.set_attribute(INPUT_TOKENS, input_tokens)
    span.set_attribute(OUTPUT_TOKENS, output_tokens)


__all__ = [
    "AGENT_NAME",
    "AGENT_NODE",
    "AGENT_STEP_TOKENS",
    "CONVERSATION_ID",
    "DATA_SOURCE",
    "EMBEDDINGS_RETURNED",
    "HITS_RETURNED",
    "INPUT_COUNT",
    "INPUT_MESSAGES",
    "INPUT_TOKENS",
    "OPERATION",
    "OUTPUT_MESSAGES",
    "OUTPUT_TOKENS",
    "PROVIDER",
    "REQUEST_MAX_TOKENS",
    "REQUEST_MODEL",
    "REQUEST_TEMPERATURE",
    "RESPONSE_MODEL",
    "RESUMED",
    "RETRIEVAL_QUERY",
    "RETRIEVAL_TOP_K",
    "TENANT",
    "TOOL_CALLS",
    "TOOL_CALL_ID",
    "TOOL_COUNT",
    "TOOL_NAME",
    "TOOL_TYPE",
    "Operation",
    "Provider",
    "model_span",
    "operation_span",
    "record_usage",
]
