# ADR-0016: Deterministic workflows before autonomous agents

- **Status:** Accepted
- **Date:** 2026-09-03
- **Phase:** 3 — Agents

## Context and problem statement

The phase is called "agents", and the word invites a particular design: give a model a set of
tools, describe the goal, and let it decide what to call and when to stop. That design demos
well. It is also the design that makes a system impossible to evaluate, because the same
question can take a different path on every run, and impossible to cost, because nothing
bounds the number of steps.

The three workflows this phase delivers do not need that freedom. Incident triage retrieves,
ranks, drafts and verifies. Postmortem drafting reads a timeline, recalls comparable
incidents, drafts sections and checks each claim against a source. Documentation gap analysis
walks a corpus and compares it against a set of expectations. In all three the steps are known
in advance; what is not known is the *content* of each step, which is exactly the part a model
should decide.

The published experience agrees. Anthropic distinguishes workflows — "predefined code paths" —
from agents where "LLMs dynamically direct their own processes", and reserves the latter for
"open-ended problems where it's difficult or impossible to predict the required number of
steps", noting agents bring "higher costs, and the potential for compounding errors". Uber's
Validator, one of the better-documented production deployments, is explicitly hybrid: a
coordinator over sub-agents where one applies LLM judgement and another runs deterministic
static linting. And Anthropic measured a multi-agent research system consuming up to fifteen
times the tokens of a chat interaction, which is the price of autonomy stated plainly.

## Decision drivers

- Phase 6 evaluates these workflows. A workflow that takes a different path each run cannot
  be scored against a golden set; it can only be sampled.
- Cost and latency must be bounded before the platform is pointed at a real corpus.
- A failure must be attributable to a step, which requires the steps to be named in advance.
- The brief asks for tool integration, and tool calling must eventually exist.
- The local model must keep working; not every model does tool calling reliably.

## Considered options

1. **Deterministic graphs now; tool calling introduced later as a capability.**
2. **Tool-calling agents from the start**: extend `ChatModel` with tools and a `ToolCall`
   result, implement it in both adapters, and let the model drive.
3. **Deterministic graphs only**, with tool calling declared out of scope for the project.

## Decision

Option 1. The three workflows are deterministic graphs whose nodes call the existing use
cases; every model call decides content, never control flow. Tool calling arrives in the last
batch of this phase as a **capability protocol** — the pattern ADR-0014 established with
`NativeHybridSearch` — so a model that supports it is used through it and a model that does
not still runs the same graphs.

Where a step can be checked without a model, it is: the citation resolver, the token budget
and the grounding check are ordinary code inside nodes. This is Uber's split, and it is also
what makes a run cheap enough to execute over a whole benchmark.

Agents compose. Incident triage is reused as a sub-component of postmortem drafting rather
than reimplemented, which is the other lesson from Uber's deployment: their Validator agent
became a component inside AutoCover, and they report that "building highly capable domain
expert agents produces superior results compared to general-purpose solutions".

## Consequences

**Positive.** Every run is reproducible at temperature zero, so the Phase 6 benchmark can
score agents the way it already scores retrieval. Cost per run is bounded by the graph, known
before the run starts, and attributable per step through `AgentStep`. A failure names the node
that produced it. And the local backend keeps working, so the repository still runs after a
clone with no cloud account.

**Negative.** These workflows cannot solve a problem their graph does not anticipate; an
operator asking something outside the three shapes gets the shape it fits worst. Adding a
capability means editing a graph rather than adding a tool description. This is a real
limitation, and it is the one this decision trades for.

**Revisit when** a workflow's graph starts accumulating conditional edges to cover cases its
author did not foresee. That is the signal that the problem has become genuinely open-ended,
and it is the point at which an autonomous loop starts paying for its cost — not before.
