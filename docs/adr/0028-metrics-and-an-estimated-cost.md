# ADR-0028: Tokens are measured, cost is estimated, and the two are labelled differently

- **Status:** Accepted
- **Date:** 2026-09-04
- **Phase:** 5 — Observability

## Context and problem statement

The spans from ADR-0026 and ADR-0027 answer *what happened in this request*. They do not
answer *what has been happening*: totalling a month's tokens by searching a month of spans is
an expensive way to compute a sum, and on any deployment that samples it is also wrong.

Phase 5 lists **cost monitoring** as a goal, which raises a question the specification declines
to answer. There is no `gen_ai` cost attribute or metric anywhere in the conventions. The
ecosystem's position is that cost is derived downstream from token counts and a price list —
which is a position, and this platform has to take one too.

## Decision drivers

- A total that disagrees with the traces is a total nobody trusts twice.
- A number that reads as measured and is estimated is worse than no number.
- Metrics and traces do not necessarily go to the same place.
- Whatever is recorded must cost nothing when observability is off.

## Considered options

For cost:

1. **Emit nothing**, and let a backend derive cost from the token metric.
2. **Emit an estimate** from a configured price list, labelled as one.
3. **Emit a cost attribute** alongside the conventions' token attributes.

## Decision

**Option 2, with the labelling doing real work.** Option 1 is defensible and is what the
conventions imply, but "cost monitoring" as a deliverable means somebody can see a number, and
pushing that entirely into a backend's configuration puts it somewhere this repository cannot
show. Option 3 is the bad one: putting cost in the `gen_ai` namespace would dress an estimate
as part of a specification that deliberately does not contain it.

So the metric is `paimon.gen_ai.cost.estimated` — our namespace, and the word *estimated* in the
name — and three properties keep it honest:

- **A model absent from the price list produces no measurement**, not a zero. Zero is a claim
  about what something cost; silence is the truth.
- **Every measurement carries the price list's revision**, and configuring prices without one is
  refused at startup. A cost figure that cannot be traced back to the prices that produced it is
  uninterpretable the moment the table changes, and the table changes.
- **The currency is on the measurement**, because a chart that mixes two is worse than no chart.

The provider's invoice remains the authority. What this buys is the *shape* of a bill before it
arrives — which tenant, which model, which day — which is the question an engineer actually has.

**Measurements come from the decorators that already record the spans.** They hold the numbers
already, so this costs a call. More importantly it guarantees the two agree: a dashboard whose
totals disagree with its traces is a dashboard nobody trusts twice, and the usual cause is two
pieces of code counting the same thing.

**Histogram buckets are set explicitly**, to the boundaries the conventions prescribe. The SDK's
defaults top out at ten thousand: for durations in seconds that is a bucket nothing ever leaves,
and for token counts it puts every large prompt in one overflow — and the large ones are the
interesting ones. Left alone, the percentiles on a dashboard would be fiction.

**Durations are recorded on failure too**, tagged with the error type. A provider timing out at
thirty seconds on every call would otherwise appear as no latency at all, because none of its
calls finished — the opposite of what an operator needs to see.

**The metrics endpoint is configured separately from the tracing endpoint.** Several tracing
backends, Langfuse among them, accept OTLP traces and nothing else. Metrics sent to one of those
fail quietly, and the symptom is an empty dashboard that looks like a platform emitting nothing.
Both providers share one `Resource`, so a backend that receives both reads them as one service.

## Consequences

**Positive.** "What did today cost, for this tenant, on this model" is a query rather than a
search. Agent runs carry a duration and an exact count of the model calls made inside them,
which is the number a cost conversation actually turns on: two agents with the same duration and
different call counts are two different bills.

**Negative.** A price list in configuration goes stale, and staleness is invisible unless
somebody looks. The revision attribute makes it *visible in the data* rather than preventing it,
which is the most a platform can do about a number it does not own.

**Discovered while building it.** Counting a run's model calls exactly needs a mutable counter
in a context variable, not an integer. The orchestrator runs nodes in separate tasks and asyncio
copies the context into each, so a child that rebound the variable would count into its own copy
and the run would report zero — while two parallel retrievals would report one call between
them. A shared object is what makes the count right for the concurrent case, which is the case
the triage agent is built around.

**Not done here.** Nothing exports a per-tenant cost *to the platform's own API*: the numbers
live in whatever backend receives them. Building a billing surface is a product decision, not an
observability one, and it is not on the roadmap.
