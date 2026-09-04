# ADR-0027: What a request looks like once retrieval, agents and tool calls are traced

- **Status:** Accepted
- **Date:** 2026-09-04
- **Phase:** 5 — Observability

## Context and problem statement

ADR-0025 built the seam and ADR-0026 put spans on the model calls. That covers the expensive
part of a request and leaves the rest of it dark: a triage run showed one span per model call
and nothing about the two retrievals that fed them, nothing about which of six nodes was slow,
and — for a call arriving over MCP — nothing at all about who ran what.

ADR-0023 also left an explicit open item: *"there is no per-tool audit log yet"*. This closes it.

## Decision drivers

- A trace should be readable as a shape, not reconstructed from a list.
- Retrieval quality questions start with "which path ran, and how many hits".
- A tool call arriving from outside is the one worth an audit record.
- The layering rule from ADR-0025 stands: only what crosses a port is instrumented.

## Decision

**Retrieval is a decorated port, exactly like the models.** Same pattern, and — importantly —
the same hazard for the second time: `VectorStore` has `NativeHybridSearch` beside it, and the
application picks its retrieval path with an `isinstance` check. A wrapper implementing only
`VectorStore` would not raise; Azure AI Search would silently stop using its own fusion and
start being fused in-process. That is a change in retrieval **quality**, invisible in every log.
Two occurrences of the same failure mode is a pattern, which is why ADR-0026 made capability
preservation a thing to test rather than a thing to remember.

Searches are traced and writes are not. Ingestion's cost is the embedding call, which has its
own span, and the write itself is a database statement — which brings the second decision.

**Database statements are instrumented.** Everything else the platform calls goes over HTTP and
was covered in the first batch; PostgreSQL does not, so a retrieval span against pgvector could
say the search took forty milliseconds and nothing about what the search did. With it,
*"retrieval was slow"* and *"the index scan was slow"* stop being the same observation — which
matters most for the one backend this platform has actually measured.

**An agent run is one span with the nodes inside it.** The run span is opened around the
generator rather than inside it, so it lasts as long as the run: a span closed at the first
yield would time the setup and nothing else. Node spans nest inside. Nesting rather than
adjacency, because a flat list makes a reader reconstruct which run each node belonged to, and
that reconstruction is the work a trace exists to have already done.

A resumed run gets its own span and its own trace, because it is a separate request minutes or
hours later. The thread id is on both as `gen_ai.conversation.id`, which is what joins them.

Node spans are named in this platform's own namespace. The conventions describe agents that
choose their own next step; these are deterministic graphs whose next step is an edge, and
bending them into `plan` or `invoke_workflow` would tell a reader something untrue about how
they work.

**A node that fails marks its span.** The adapter deliberately catches a node failure and turns
it into a recorded step rather than raising (ADR-0016 wanted a run that fails to still be
investigable). Without recording it on the span too, the trace would show a node that took some
time and succeeded — the exact opposite of what happened.

**Tool calls are audited at the gateway.** Every call an external MCP client makes gets an
`execute_tool` span carrying the tool, the call id and the tenant. At the gateway rather than in
the executor for two reasons: the executor is agent logic and does not import instrumentation,
and the gateway is the edge where a call arrives from *outside*, which is the one worth an audit
record. Tool calls an agent makes to itself are covered by their node's span.

**The tenant goes on these spans.** It is not in the conventions and it is the single most
useful attribute a multi-tenant platform can record: a question about one organization's traffic
— its cost, its latency, its failures — cannot be answered without it.

**Arguments and query text stay off.** Tool arguments are the caller's data; a query is what
somebody asked. The query is recorded only under the same `capture_content` switch that gates
prompts, which is off by default and refused when deployed.

## Consequences

**Positive.** A triage run now reads as a shape: one run span, six node spans, and inside the
ones that did work, the retrievals and the model calls with their HTTP attempts under them. The
question "why did this run take eleven seconds" is answerable by looking rather than by
correlating timestamps across two log streams.

**Negative.** Span volume grows with graph size — a six-node run is eight or more spans before
the model and retrieval calls under them. The sampling ratio from ADR-0025 is the control, and
it is head-based, so a sampled run is a complete run.

**Discovered while writing it.** The capability hazard appearing twice was not a coincidence:
this platform expresses optional behaviour as protocols on purpose (ADR-0003, ADR-0018), so any
decorator over any of its ports has to be checked for it. That is now the first thing the tests
for a new wrapper assert.

**Not done here.** Metrics and cost. These spans carry token counts, which answers "what did
this request cost" and not yet "what did today cost" — an aggregation question that wants a
metric rather than a search over spans. That is the next batch.
