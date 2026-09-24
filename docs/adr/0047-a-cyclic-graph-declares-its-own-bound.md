# ADR-0047: A cyclic graph declares its own bound

- **Status:** Accepted
- **Date:** 2026-09-24
- **Deciders:** Sergio Medina
- **Phase:** 9 — Autonomy, and whether it pays for itself

## Context and problem statement

Every graph before this phase was acyclic. `GraphSpec`
([ADR-0015](0015-agent-state-lives-in-the-domain.md)) describes a graph in the domain's own
vocabulary and knows nothing about the framework that runs it; the LangGraph adapter compiles
it and passes a `recursion_limit` so a runaway graph stops rather than running forever. With
no cycles, that limit was never near: a graph's worst case is its node count.

The investigator loops. `act → tools → act` runs until the model stops asking for tools, so
its cost is a function of `max_turns` — a builder argument — and not of its shape. Two turns
and eight turns are the same graph.

That breaks an assumption nothing had needed to state. A step limit set for acyclic graphs
silently truncates a loop, and a truncated loop does not look like an error: it looks like an
agent that stopped early. The first thing the measurement would have had to rule out is the
harness cutting the loop — and in fact that is exactly the alternative explanation that had
to be excluded when the local model took one hop in 100 of 100 runs.

**Step 0 of this phase asked whether `GraphSpec` and the adapter support cycles at all.** They
do; the shape is expressible and the adapter compiles it. What was missing was the bound.

## Decision drivers

- A graph must not be able to declare a loop the orchestrator will silently cut.
- The domain must not learn about the framework. An agent that had to be told the step limit
  in order to validate itself would know about the thing ADR-0015 exists to hide.
- The failure must be loud and early — at build time, in the graph's own vocabulary, not a
  stack trace of framework frames during a benchmark.
- Acyclic graphs must not pay for this.

## Considered options

1. **Raise the adapter's limit for everyone.** One number, high enough for the loop.
2. **Pass the framework's limit into the builder**, and let each agent check its own budget
   against it.
3. **Let a graph declare its worst case**, and have the adapter reconcile that with its limit.

## Decision

**Option 3.** `GraphSpec` carries `worst_case_steps`, and the investigator computes it as
`max_turns * 2 + 3` — two node executions per turn, plus `finalize`, `verify` and the closing
step. Zero means "this graph does not know", which is the honest answer for an acyclic graph
whose worst case is its node count and is already covered.

The bound is **declared, not computed**, because only a graph with a loop knows it: it is a
function of the loop's own budget, which is an argument to the builder and invisible in the
shape. It is declared **on the spec** rather than passed to the adapter, because the
alternative is an agent that must be handed the framework's step limit in order to validate
itself.

`validate()` rejects a negative bound. The deployment's `agents.step_limit` (25) is checked
against the investigator's worst case (19 at the default 8 turns), so a configuration that
would truncate the loop fails before a run starts rather than during one.

## Consequences

### Positive

- A loop that would be cut is a configuration error at build time, with both numbers named.
- The measurement could rule out the harness as the cause of one-hop behaviour: no limit
  fired in 100 of 100 runs, every run reached its second `act`, and the runs used 5 of 25
  available steps. That left the model as the explanation, which the isolation run then
  confirmed directly.
- Acyclic graphs are unchanged and declare nothing.

### Negative

- The bound is a promise the graph makes about itself, and nothing proves it. A builder that
  computes it wrongly gets a limit that is wrong in the same direction. A test pins the
  arithmetic; the arithmetic is still written twice, once in the builder and once in the test.
- One more field on a domain type that exists because of how the framework behaves, which is
  a small leak in the direction ADR-0015 is trying to seal.

### Neutral

- Zero as "unknown" rather than "no steps" is a sentinel. It is documented on the field, and
  the only alternative — `int | None` — buys nothing here, since a graph with no cycle has no
  bound worth stating.

## Alternatives in detail

**Option 1 (raise the limit globally)** removes the symptom and the signal together. A limit
high enough for an eight-turn loop is high enough to let a genuinely runaway graph spin for a
long time before anything notices, and every acyclic graph loses the protection it had.

**Option 2 (pass the limit in)** was close. It was rejected because the direction of knowledge
is wrong: the agent would have to be told the framework's number to decide whether its own
budget fits, so every builder would take a parameter that exists only because of the
orchestrator. Under option 3 the agent states a fact about itself and the adapter, which
already knows the framework, does the reconciling.
