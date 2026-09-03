# ADR-0015: Agent state lives in the domain, the graph lives in infrastructure

- **Status:** Accepted
- **Date:** 2026-09-03
- **Phase:** 3 — Agents

## Context and problem statement

Phase 3 introduces LangGraph. LangGraph is not a library the platform calls occasionally; it
is a control-flow framework that wants to own the shape of the code written inside it. The
canonical example defines the state as a `TypedDict`, writes node bodies against that dict,
and composes them with `StateGraph`. Follow it literally and the business logic of every
agent — what to retrieve, what to draft, when to stop — is expressed in the vocabulary of a
third-party package and can only be executed by that package's runtime.

That collides with what the first two phases established. Every port so far has two
implementations passing one contract suite, and every use case is testable without the
frameworks around it. An agent layer that cannot be tested without a graph runtime would be
the first part of this codebase where the framework is not replaceable, and it would be the
part where correctness matters most, because an agent takes several steps and each one can
be wrong in a way the next one hides.

Anthropic's own guidance is blunt about the risk: framework abstractions "can obscure the
underlying prompts and responses, making them harder to debug", and the recommendation is to
understand the mechanics before adopting one.

Against that stands an equally real fact: LangGraph earns its place. Checkpointing, resuming
a suspended run, `interrupt` for human-in-the-loop, step streaming and retry semantics are
genuinely hard, and reimplementing them to avoid a dependency would be the worse mistake.

## Decision drivers

- A node must be callable and assertable in a unit test without a graph runtime.
- The framework's durability machinery must be used, not reimplemented.
- The boundary must be verifiable by machine, in the style of the other five contracts, and
  not maintained by discipline.
- Idiomatic LangGraph should still be recognisable to someone who knows LangGraph.

## Considered options

1. **State and nodes in `agents/` as plain Python; the graph built in `infrastructure/`.**
2. **State as a LangGraph `TypedDict`, nodes written against the framework.** Idiomatic and
   fastest to write; direct access to `interrupt` and streaming inside node bodies.
3. **A domain state mapped to and from a separate graph state at the boundary.** Full
   isolation, at the cost of a translation layer for every field.
4. **No framework: hand-written orchestration.** Maximum control; means writing a
   checkpointer, a resume protocol and a scheduler by hand.

## Decision

Option 1, made possible by a detail of LangGraph that option 3 was written to work around:
**a `StateGraph` accepts a dataclass as its state schema**, not only a `TypedDict`.

So `paimon.domain.agents` holds a plain `AgentState` dataclass, plain reducer functions and a
`GraphSpec` describing an agent's shape as data; `paimon.agents` holds the agents themselves,
as node bodies of the form `async def node(state: AgentState) -> StateUpdate`.

That split was not the original placement. Everything started in `paimon.agents`, and the
import-linter contract rejected it the moment the adapter was written: an adapter compiling a
`GraphSpec` has to import one, and `paimon.agents` sits above `paimon.application`, so
infrastructure would have been importing a layer above itself. The correction is the one the
failure pointed at — the vocabulary an agent is *described in* is domain, because the
`AgentWorkflow` port is written in it and any adapter must speak it; the agents themselves are
the layer above the use cases they orchestrate. The contract found a contradiction between
this ADR's own title and where the code had been put. Reducers are attached
with `Annotated[tuple[AgentStep, ...], append_steps]` — and `Annotated` is `typing` from the
standard library, so declaring how concurrent writes merge costs the package no dependency
at all. `paimon.infrastructure.orchestration` builds the `StateGraph`, owns the checkpointer,
the interrupts and the streaming.

Nodes return a **partial** `StateUpdate` rather than a whole state. A node that returns
everything has to decide what to do with fields it never examined, and the honest answer —
leave them alone — is what a partial update expresses without having to be stated. The
`TypedDict(total=False)` makes the type checker reject a key that is not a state field,
which catches the typo that would otherwise be discarded silently at runtime.

A sixth import-linter contract, *"Agent logic does not import the orchestration framework"*,
forbids `langgraph`, `langchain` and `langchain_core` inside both `paimon.agents` and
`paimon.domain.agents`. A unit test
asserts that `StateUpdate` covers exactly the fields of `AgentState`, so the two cannot drift.

## Consequences

**Positive.** Node bodies are pure functions of their input: a test constructs a state, awaits
the node and asserts on the update, with no runtime involved. The reducers — the part most
likely to be subtly wrong, because they only matter when branches run concurrently — are
ordinary functions with ordinary tests. The framework stays swappable in the one direction
that matters: if LangGraph is replaced, the agents' logic is unaffected and only the adapter
is rewritten. And the rule is enforced by CI rather than remembered.

**Negative.** This is not what a LangGraph tutorial looks like. A contributor arriving from
the documentation will look for the graph inside the agent and not find it. Node bodies
cannot call `interrupt` directly, so suspension is expressed as state (`awaiting`) and acted
on by the adapter — one indirection where the framework offered none. A dataclass state is
marginally slower to copy than a `TypedDict`, which is irrelevant next to a model call.

One concession the framework extracted, recorded because it looks like noise and is not: a
node cannot be typed as `Callable[[AgentState], Awaitable[StateUpdate]]` where LangGraph
expects it. Its node protocol names the parameter `state`, and a bare `Callable` has no
parameter names, so the alias is rejected for a reason unrelated to behaviour. The adapter
declares a one-method protocol with the parameter named instead.

**Accepted risk.** LangGraph's dataclass support is documented but less travelled than the
`TypedDict` path; a bug there would be felt here first. The contract suite and the unit tests
around the reducers are what would catch it.
