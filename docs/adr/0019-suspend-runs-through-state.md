# ADR-0019: A run suspends by writing state, not by calling the runtime

- **Status:** Accepted
- **Date:** 2026-09-03
- **Phase:** 3 — Agents

## Context and problem statement

Human-in-the-loop is the feature the production deployments studied for this phase actually
value: Replit lets a person watch an agent install packages and intervene; LinkedIn and Elastic
put a reviewer in the path. It is also the one that collides hardest with ADR-0015.

LangGraph implements it with `interrupt(value)`, called from inside a node. The call raises the
first time, the framework persists the graph's state, and on resume the same node runs again
from the top with `interrupt` returning the answer instead of raising. It is a good design, and
it requires the node to import the framework and to be executed by it — which is precisely what
ADR-0015 spent effort avoiding, and for a reason that has not changed: a node that calls
`interrupt` cannot be tested without a graph.

ADR-0017 also left a promise outstanding. It deferred `psycopg` until something actually needed
graph state; resuming does, so this is where that debt is paid.

## Decision drivers

- Node bodies must stay callable and assertable without a runtime.
- The framework's suspension machinery must be used, not reimplemented.
- A run waiting for a person must be visible as such, or nobody will answer it.
- A deployment that never suspends a run should not pay for the machinery.

## Considered options

1. **Nodes write `awaiting` to the state; the adapter interrupts.**
2. **Nodes call `interrupt` directly**, relaxing ADR-0015 for the nodes that need it.
3. **Suspend outside the graph**: end the run, and start a second run with the decision.

## Decision

Option 1. A node that wants a person sets `awaiting` to the question. The adapter's node wrapper
— which is already there to time the node and record its step — sees the field and calls
`interrupt`, then puts the answer back into the state as `decision`.

The result: `paimon.agents` still imports no framework, every node is still an ordinary
coroutine, and the framework's checkpointing, replay and resume are used rather than rewritten.

Option 3 was rejected because it produces two runs where the operator has one question. The
trace splits, the cost splits, and correlating them afterwards is work the platform would be
creating for its own users.

`RunStatus.AWAITING_INPUT` exists for this and is deliberately not terminal, a decision made in
the phase's first batch before there was anything to suspend. A poller that treats a suspended
run as finished never comes back for the decision.

**Resumability is off by default.** It costs a second connection pool on a second driver, and a
deployment that never suspends a run should not pay for it. A workflow built without a graph
saver runs identically and refuses to resume with a message that says why, rather than failing
inside the framework about a missing checkpointer.

The graph checkpointer is given an **explicit type allowlist** rather than the default
permissive serializer. Reviving an arbitrary class out of a checkpoint is code execution for
anyone who can write to that database, and the framework warns that its permissive default is
temporary. Four domain types are listed, all frozen dataclasses of plain values; adding a type
to the graph's state now means adding it there and being asked why it belongs in a checkpoint.

## Consequences

**Positive.** Suspension is testable at both levels: a node returning `awaiting` is asserted
without a runtime, and the round trip is exercised against a real in-memory saver. The review
step is configuration, so the postmortem agent can be benchmarked unattended and reviewed in
production without two code paths.

**Negative, and worth stating precisely.** The suspending node runs twice — once to ask, once
after the answer — because that is how `interrupt` replays. Node bodies are pure, so this is
wasteful rather than wrong, but **a node that calls a model must not be the node that
suspends**, and nothing enforces that beyond this sentence and the review node being trivial.

A related surprise, recorded because it looks like a bug: the suspending node's step appears
only in the resumed half of the trace. Interrupting discards that node's work, so there is no
step to record until it runs again.

And `AgentState`'s fields all carry defaults now, including `question` and `tenant_id`. The
orchestrator constructs the state schema with no arguments when rebuilding from a checkpoint,
so a required field made every resumed run a `TypeError` from inside the framework. The
invariant is kept where it can be: `stream` always supplies both, and `AgentRun` — the record a
person reads — still refuses a blank tenant outright.
