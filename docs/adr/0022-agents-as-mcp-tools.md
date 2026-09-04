# ADR-0022: Agents are MCP tools that run to completion; documents are not resources

- **Status:** Accepted
- **Date:** 2026-09-04
- **Phase:** 4 — MCP

## Context and problem statement

ADR-0020 put two tools on the MCP endpoint: search the corpus, and read a document. Both
are retrieval. The agents built in Phase 3 — triage, postmortem, documentation gaps — were
reachable over HTTP and nowhere else, which is the wrong way round: an agent that already
knows how to search, ground and verify is exactly what an external assistant should be able
to delegate to, and it is far more valuable to it than raw passages it must interpret itself.

Two questions had to be answered to expose them. How does a run reach a client that speaks a
request/response protocol — and how are documents addressed, given that MCP has a resource
concept that looks made for them?

A third problem surfaced while answering the first, and it was ours rather than the protocol's:
**a run recorded what it did but not what it produced.** `AgentRun` carried its steps, their
timings and their token counts; the answer itself was streamed to whoever was listening and
then dropped. Over HTTP that was survivable, because the caller is the listener. Over MCP it
is not: the tool call has to return the answer, and there was nothing to return.

## Decision drivers

- A tool call returns once; a run is not a stream from the caller's side.
- What a run produced belongs to the run, not to whoever happened to be watching it.
- Every tool call must establish the caller's tenant before it reads anything.
- A test that passes while the deployed wiring is broken is worse than no test.

## Considered options

For running an agent:

1. **Start and poll** — a tool that returns a run identifier, and a second that reports on it.
2. **Run to completion** — one tool call, one answer.

For documents:

3. **A resource template** — `paimon://documents/{document_id}`, addressable by URI.
4. **Keep `read_document` as a tool.**

## Decision

**Option 2 for agents.** These graphs are deterministic and step-limited, so a run's cost is
bounded before it begins and it finishes in seconds. Handing a client an identifier and making
it poll for a result it will have almost immediately gives it work to do for no reason and
doubles the surface it must implement. A workflow whose cost is *not* bounded should not be
exposed this way — which is a reason to keep them bounded, not a reason to start with polling.

The tool returns the answer **and** the steps. A model deciding whether to trust an answer is
better off seeing that retrieval found four passages across two documents — and much better off
seeing when it found none.

**Option 4 for documents, and this one is a limitation rather than a preference.** A resource
template's function is wrapped in pydantic's `validate_call`, which revalidates its arguments;
a revalidated `Context` is a copy that has lost its binding to the request. Reading
`context.headers` inside a template therefore raises *"Context is not available outside of a
request"* in the middle of an actual request. No request means no bearer token, and no token
means no tenant. A resource that served documents without first establishing whose they are is
not a feature worth having, so `read_document` stays a tool, where the context does survive.
The reasoning sits in `interfaces/mcp/server.py` next to where the resource would go, and a
test asserts the server offers no resources at all — so that when the SDK exposes request state
to templates, the absence is found on purpose rather than rediscovered as an oversight.

**And the answer is now part of the run.** `AgentRun.answer` is written by the orchestrator as
the graph streams — the last node to produce a draft owns it, and a node that withdraws a draft
writes the withdrawal, so a refusal replaces the text it refused rather than leaving both on the
record — persisted alongside the steps, and returned by the HTTP API too. The migration adds the
column as `NOT NULL DEFAULT ''` rather than nullable: runs recorded before it produced an answer
that was never kept, and `""` says that honestly where `NULL` would invite a caller to guess
whether the run had failed.

## Consequences

**Positive.** An external assistant can now delegate a whole task — "triage this" — instead of
fetching passages and doing the reasoning itself, and it gets back both the answer and the
evidence of how it was reached. Runs started over MCP are readable over the HTTP API, because
there is one record and it is not owned by the protocol that happened to start it.

**Negative.** A tool call now holds a connection for the length of a run. That is acceptable at
the current bounds and would not be if a graph grew unbounded; the step limit is what keeps this
decision honest, so relaxing it is a decision about this endpoint too.

**Discovered here, and worth recording.** The gateway the tests use is substituted for an
in-memory one, and the gateway the composition root builds was, for a while, built without the
agents — every test passed and the deployed endpoint would have answered *"this deployment
offers: none"*. The fix was one argument; the lesson is that a substituted collaborator hides
the wiring that builds the real one, so there is now a test that asks the application for its
own gateway and checks the agents are in it.
