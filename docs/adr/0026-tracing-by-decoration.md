# ADR-0026: Model calls are traced by wrapping the port, not by editing the adapters

- **Status:** Accepted
- **Date:** 2026-09-04
- **Phase:** 5 — Observability

## Context and problem statement

ADR-0025 settled what the platform emits and who may emit it: plain OpenTelemetry, from the
layers that perform I/O. This decides the mechanics for the largest group of those — the model
calls, which are the expensive part of every request and the part a reader most wants a
timeline of.

There are two chat adapters and two embedding adapters today, one pair speaking the OpenAI wire
format at any endpoint and one pair speaking Azure OpenAI. There will be more, because that is
the point of ADR-0003. The question is where the instrumentation goes.

## Decision drivers

- The same span, produced the same way, whichever adapter is configured.
- An adapter added later should be traced without its author having to know.
- Tracing must observe the call, never change what it returns or what it raises.
- The tool-calling capability is checked with `isinstance` and must survive.

## Considered options

1. **Instrument inside each adapter** — a span opened where the HTTP call is made.
2. **Decorate the port** — a wrapper implementing `ChatModel` / `EmbeddingModel`, composed at
   the composition root around whatever adapter was selected.

## Decision

Option 2.

Instrumentation inside each adapter is the same twenty lines four times, with four chances to
drift and a fifth waiting for the next adapter. Worse, it makes tracing an author's
responsibility: a new adapter is traced if somebody remembers, and the failure mode of
forgetting is a gap in a dashboard that nobody notices because nothing broke.

A decorator makes it a property of the object graph. The composition root wraps whatever it
built; the wrapper is the only place that knows the conventions; and a fake in a test is
wrapped by the same code as a real adapter, so the assertions about spans are assertions about
production behaviour.

There is a second, less obvious gain. The span covers the whole **logical** call, and the httpx
instrumentation from the previous batch opens a child span per HTTP attempt. So a retry appears
*inside* the model call rather than beside it, and "the model took nine seconds" and "the model
took three attempts" are visible at the same time. Instrumenting inside an adapter, at the
point where the request is actually sent, would have produced one span per attempt and no
parent — the same information, arranged so that nobody can read it.

**Attribute names live in one module.** The GenAI conventions are still marked *Development*
and have already renamed `gen_ai.system` to `gen_ai.provider.name` and `prompt_tokens` to
`input_tokens`. Every name this platform writes is a constant in `observability/genai.py`, so
the next rename is one file.

**Operations and providers are enumerations, not strings.** These values are what a backend
filters and charts by, and a typo does not fail — it produces a category that quietly contains
one span. `Provider.OPENAI` names the **API dialect** rather than a company: the non-Azure
adapter speaks the OpenAI wire format at whatever endpoint it is pointed at, the registry has no
value for "OpenAI-compatible", and naming the dialect is the honest reading of an attribute
whose job is telling a reader what the call looked like.

**The capability hazard, which is the part that would have hurt.** `ToolCallingChatModel` is a
capability this platform tests for with `isinstance` (ADR-0018). A decorator implementing only
`ChatModel` would silently *remove* it from an adapter that had it — and nothing would raise.
Agents would simply stop being offered tools, everywhere, and the platform would keep answering.
So the wrapper is chosen by what it is given, one wrapper satisfies both protocols, and a test
asserts the capability survives wrapping and is not invented for a model that lacks it.

**What is recorded beyond the conventions**, under a `paimon.` prefix so it is obvious which is
ours: the number of tool definitions sent, because they are sent on every turn and are therefore
input tokens paid again each time — the usual explanation for a conversation whose cost climbs
with nothing else on the span to show it; the names of the tools the model asked for, because
that is the shape of its reasoning; and for embeddings, how many texts went in and how many
vectors came back, because an ingestion that is slow from sending one text per request looks
exactly like one that is slow because the provider is.

**Arguments are not recorded, and neither is what is embedded.** Tool arguments are the caller's
data. What is embedded during ingestion is the corpus itself, so recording it would copy an
organization's documentation into a tracing backend one chunk at a time — which is why the
embedding wrapper has no content switch at all, where the chat wrapper has one that is off by
default and refused when deployed.

## Consequences

**Positive.** Adding an adapter costs no instrumentation. Changing a convention costs one file.
And because the wrappers sit over the ports, the fakes are traced too, so a test can assert on
spans without a model server.

**Negative.** A layer of indirection between the composition root and the adapter, which shows
up in a stack trace and in a debugger. It is one frame, and it is named for what it does.

**Discovered while writing it.** `ToolCallingChatModel` declares only the tool method — it is a
capability added to a chat model, not a replacement for one — so neither protocol alone
describes what the tool-calling wrapper holds. Naming the conjunction is what let the type
checker agree the wrapping was safe, and it is a more honest description of the thing than
either protocol was on its own.

**Not done here.** Retrieval and agent spans, and the MCP tool-call audit trail promised in
ADR-0023, are the next batch. Metrics and cost are the one after: these spans carry token counts
as attributes, which answers "what did this request cost" and not yet "what did today cost".
