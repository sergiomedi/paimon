# ADR-0018: Tool calling is a capability, and the tool surface stays small

- **Status:** Accepted
- **Date:** 2026-09-03
- **Phase:** 3 — Agents

## Context and problem statement

ADR-0016 committed to deterministic workflows and deferred tool calling to a capability
protocol. This is that decision, and it has two halves that are usually conflated: how a model
is *offered* tools, and what tools exist at all.

The first half is a compatibility question. Function calling is the industry standard and the
brief asks for tool integration, but it is not universally supported and — more to the point —
not universally supported *well*. Several of the local models this platform is meant to run on
emit malformed argument objects, invent tool names, or answer in prose when asked to call
something. A platform that assumes tool calling excludes them; a platform that detects it with
a boolean makes every caller remember to check.

The second half is a design question, and the more consequential one. Every tool added is a
tool that every call has to be given a reason not to choose. Anthropic's guidance is explicit
that tool design deserves the engineering a prompt gets and should follow poka-yoke principles
— make misuse structurally difficult rather than documented against.

## Decision drivers

- The local backend must keep working, so the repository still runs after a clone.
- A tool a model cannot be trusted to call correctly must fail loudly, not silently.
- Phase 4 exposes platform capabilities over MCP; the same declarations should serve both.
- No prompt is a security boundary.

## Considered options

1. **A separate `ToolCallingChatModel` protocol**, satisfied by adapters that support it.
2. **Extend `ChatModel` with an optional `tools` argument**, and have models that cannot do it
   ignore the argument or raise.
3. **A `supports_tools` boolean** on `ChatModel`.

## Decision

Option 1, the pattern ADR-0014 established with `NativeHybridSearch`. `ToolCallingChatModel`
declares `complete_with_tools`; both chat adapters implement it and both pass the same contract
suite. A caller that does not care never learns which model it holds; a caller that does asks
the type checker rather than a flag.

Arguments are parsed at the adapter boundary. Providers send them as a JSON string, and one
place deciding what malformed JSON means is better than every caller deciding separately — the
answer being that it is an error. A tool run with no arguments because its arguments could not
be read is how a search for nothing gets reported as a search that found nothing.

A response carrying neither text nor a tool call is an error rather than an empty answer, for
the same reason: a caller cannot tell silence from a conclusion and will present the first as
the second.

**The tool surface is two tools**, `search_corpus` and `read_document`, and a unit test asserts
the count so that growing it requires an argument. Both are read-only. The tenant is bound when
the executor is constructed and is never read from a tool call, because a model that could name
the tenant it wanted to search could ask for another organization's runbooks.

Three behaviours in the executor are deliberate and easy to mistake for fussiness. An empty
search result says so *and* says not to answer from memory, because a model given silence and
no instruction fills it. A document longer than the budget is truncated with the truncation
announced, because a model that cannot tell it received part of a procedure will describe the
part it got as the whole of it. And an out-of-range numeric argument is clamped rather than
rejected — a model asking for fifty passages has misjudged, not malfunctioned — while an
argument that is not a number at all is refused.

## Consequences

**Positive.** The three workflows are unchanged and still deterministic: this adds a capability
without spending it. The declarations are data, so Phase 4 can expose the same two tools over
MCP from one definition rather than a second that drifts. Both adapters pass one contract, so a
difference between the local endpoint and Azure surfaces as a failing test rather than as an
agent that behaves differently in production.

**Negative.** Nothing in the platform calls `complete_with_tools` yet. That is a capability
with no consumer, which is ordinarily a smell; it is accepted here because the consumer is
named and imminent — Phase 4's MCP server — and because building the capability with the
protocol work fresh is cheaper than retrofitting it around an MCP integration.

**Revisit when** an agent needs a step whose shape cannot be known in advance. That is the
condition ADR-0016 named for autonomy, and it is the point at which these tools stop being a
capability and start being a loop.
