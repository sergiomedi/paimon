# ADR-0008: Target domain — engineering operations

- **Status:** Accepted
- **Date:** 2026-09-01
- **Phase:** 1 — Foundation

## Context and problem statement

The platform is described in the brief as operating on generic "organizational knowledge".
That framing is sufficient for architecture but insufficient for everything that follows it:
a retrieval system cannot be evaluated without a corpus, agents cannot be designed without
tasks that have verifiable outcomes, and an evaluation framework (Phase 6) requires ground
truth that only a concrete domain supplies.

A generic demonstration also fails at the thing this repository is meant to do. "It answers
questions about your documents" is unfalsifiable. "It resolves this incident from these
runbooks, with citations, and here is the measured faithfulness" is not.

## Decision drivers

- A publicly available corpus, so the repository can be cloned and run by anyone.
- Tasks with checkable outcomes, so agent behaviour can be evaluated rather than admired.
- Natural fit with the Phase 4 MCP tools (PostgreSQL, filesystem, GitHub).
- Domain knowledge the author can judge, since evaluation requires knowing the right answer.

## Considered options

1. **Engineering operations** — runbooks, postmortems, ADRs, API documentation, incident
   history.
2. **Industrial technical documentation** — equipment manuals, maintenance procedures,
   safety regulations.
3. **Legal and compliance** — internal policies, contracts, regulation.

## Decision

Engineering operations.

The corpus is assembled from openly licensed sources: public runbooks and SRE material,
published postmortems, open-source project documentation and ADR collections. It is
reproducible by anyone cloning the repository, which is a precondition for the benchmarks
to be credible.

The agent tasks the domain supplies are concrete and gradable:

- **Incident triage** — correlate a symptom against runbooks and past postmortems, and
  propose a diagnosis with citations.
- **Postmortem drafting** — assemble a timeline from incident records into a structured
  document.
- **Documentation gap analysis** — identify procedures referenced but never documented.

Each has a checkable output, which is what makes the Phase 6 metrics — faithfulness,
groundedness, relevance — measurable rather than decorative.

The domain also aligns with the Phase 4 MCP integration: a GitHub tool and a PostgreSQL
tool are *natural* here rather than contrived, because incident context genuinely lives in
repositories and operational databases.

## Consequences

### Positive

- A reproducible, openly licensed evaluation corpus.
- Agent outputs that can be scored against ground truth instead of judged by vibes.
- MCP tools that are motivated by the domain rather than bolted on to satisfy a phase.
- The demonstration is legible to exactly the audience the repository targets.

### Negative

- Narrows the apparent generality of the platform. Mitigated by keeping the domain out of
  the core: ingestion, retrieval and orchestration know nothing about incidents; the domain
  lives in the corpus, the prompts and the evaluation set.
- Public runbooks and postmortems are heterogeneous in structure, which makes chunking
  harder than a uniform corpus would. Accepted — it is also more representative of reality.

### Neutral

- Corpus curation is ongoing work through Phase 2, not a one-off task.

## Alternatives in detail

### Option 2 — Industrial technical documentation

The most convincing enterprise narrative of the three, and the closest to how such a
platform would be sold. Rejected on corpus availability: high-quality equipment manuals are
overwhelmingly proprietary, and a benchmark built on documents nobody else can obtain is not
a benchmark. Would become the strongest option given access to a real document set.

### Option 3 — Legal and compliance

Genuinely well matched to the technical requirements — citation support and faithfulness
matter more here than anywhere else, which would justify the entire evaluation framework.
Rejected on two grounds: the space is saturated with retrieval demonstrations, so it
differentiates poorly, and evaluating legal answers correctly requires domain expertise the
author does not have. Judging one's own system wrongly is worse than not judging it.
