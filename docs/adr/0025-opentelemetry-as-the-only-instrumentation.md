# ADR-0025: OpenTelemetry is the instrumentation; a backend is a destination

- **Status:** Accepted
- **Date:** 2026-09-04
- **Phase:** 5 — Observability

## Context and problem statement

The platform can already say what it did — every run records its steps, their durations and
their token counts, and every log line carries a correlation id. What it cannot say is where
the time went inside a request, or which of four concurrent model calls was the slow one.
That is what tracing is for, and Phase 5 is where it lands.

Two questions have to be answered before any span is written, and answering them in the wrong
order is how observability work turns into vendor migration work eighteen months later.

**What does the code emit?** A vendor's SDK, or plain OpenTelemetry. **And who is allowed to
ask for a span?** Every layer, through some abstraction, or only the layers that do I/O.

## Decision drivers

- The phase exists to make the platform legible, not to acquire an owner.
- Prompts and completions are somebody else's documentation and somebody else's words.
- A tracing backend that stops answering must not become this platform's outage.
- The existing architecture contracts must survive the addition, not be relaxed for it.

## Considered options

For what is emitted:

1. **Langfuse's SDK**, decorating the call sites that matter.
2. **Plain OpenTelemetry**, exported over OTLP to whatever backend is configured.

For who may trace:

3. **A `Tracer` port** in the domain, implemented by an OTel adapter and passed to use cases.
4. **Instrument only what crosses a port** — adapters and interfaces — and leave the domain
   and the use cases untouched.

## Decision

**Option 2.** Langfuse accepts OTLP at a documented endpoint and reads the `gen_ai.*`
semantic conventions directly, so its SDK buys convenience and costs the ability to leave. With
plain OpenTelemetry the backend is an endpoint and a credential: Langfuse today, Azure Monitor
in Phase 7 when there is an Azure deployment to attach it to, or a collector fanning out to
both, with no change to a single call site. Adopting a vendor's SDK in the phase whose purpose
is seeing clearly would be an odd place to give up the ability to choose who does the showing.

The cost is real and worth stating: the GenAI semantic conventions are still **Development**,
not Stable, and have already renamed things — `gen_ai.system` became `gen_ai.provider.name`,
`prompt_tokens` became `input_tokens`. Writing to them means tracking a moving target. Writing
to a vendor's SDK means tracking a moving target *and* a vendor. The conventions are the one
that ends up standardised.

**Option 4, and this is the part that keeps the architecture intact.** What is worth tracing is
exactly what crosses a port: a model call, a vector search, an HTTP request, a node in a graph.
The domain performs no I/O, so it has nothing to trace, and a `Tracer` threaded through use
cases would be an abstraction over an API that is already an abstraction — paid for in every
signature it passed through, and in a fake in every test that constructed one.

There is a second reason it works here. The orchestration adapter already times each node and
attributes its tokens, because Phase 3 needed that for the run record. Tracing is exporting a
seam that exists rather than cutting a new one.

Consequently `import-linter` needs no new exception: instrumentation lives in
`paimon.observability` and is imported by infrastructure and by the composition root, which is
what the existing contracts already permit.

**Off by default.** No endpoint, no provider — and every `start_as_current_span` elsewhere then
runs against the API's own no-op, which costs almost nothing and keeps `if tracing_enabled:`
out of the code doing the work. Enabling tracing without an endpoint is refused at startup,
because a process that collects into nowhere looks exactly like one that traces correctly until
somebody opens the backend.

**Content capture is off, and refused outright in deployed environments.** The conventions mark
message content opt-in and warn that it is likely to carry sensitive data. Here that content is
an organization's internal documentation and whatever its people typed into a box. Exporting it
to a third party should cost a code change and a review; this guard is what makes it cost one,
and it is the same shape as the guard that already refuses SQL echo outside local environments.

**Sampling is head-based and parent-respecting.** A service that re-decides half way through a
trace produces traces with holes in them, which are harder to read than traces that were never
kept.

## Consequences

**Positive.** Logs and traces join in both directions: every log line carries the trace and
span it happened inside, and every request's span carries the correlation id the logs are keyed
by. Everything reaching the network through httpx — every model provider, both vector stores
and the MCP client — is covered by one call, and so is whatever is added next.

**Negative.** A moving specification, as above. And a batching exporter means the newest spans
are the ones most likely to be lost if the process dies badly, which is a poor property for the
spans describing a process dying badly; the shutdown path flushes, and a hard kill does not
get to.

**Discovered while building it.** The ASGI instrumentation opens a child span for every `send`
and `receive` message by default: four spans per plain JSON request where one is meaningful,
and on a streaming endpoint a count that grows with the length of the answer. Every one of them
is stored and billed by whoever receives it. They are switched off, and a test pins the count
at one, because that is exactly the kind of default that comes back on a dependency bump.

**Not done here.** Model, agent and retrieval spans — the `gen_ai.*` conventions themselves —
are the next batch. Metrics and cost are the one after. This batch is the seam: a provider, an
exporter, the join with logging, and the two spans that come from libraries.
