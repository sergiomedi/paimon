# ADR-0012: Fuse retrieval results by rank, not by score

- **Status:** Accepted
- **Date:** 2026-09-02
- **Phase:** 2 — RAG

## Context and problem statement

Hybrid retrieval runs two retrievers over the same corpus and has to return one ordering.
Dense retrieval finds paraphrase, which is most of how people ask questions; lexical
retrieval finds exact tokens — an error code, a flag, a hostname — which an embedding of a
rare string carries almost no signal for. Each misses what the other catches, so both run,
and something has to combine them.

The combination is where it goes wrong. The two retrievers do not produce comparable
numbers: BM25 has no upper bound and shifts with corpus statistics, while cosine similarity
is confined to [-1, 1] and is usually crowded into a narrow band near the top. Any
arithmetic over those two quantities is arithmetic over different units.

## Decision drivers

- The fused ordering must not be decided by which retriever's numbers happen to be larger.
- Local and cloud backends must order comparably, or the Phase 6 benchmark measures fusion
  algorithms instead of retrieval backends.
- Fusion must be deterministic; an evaluation that reorders equal hits between runs reports
  noise as a change.
- A surprising result must be explainable after the fact.

## Considered options

1. **Reciprocal Rank Fusion**: each list contributes `weight / (k + rank)`, summed per chunk.
2. **Normalized score fusion**: min-max or z-score each list, then a weighted sum.
3. **Cascade**: retrieve lexically, then re-rank the candidates by embedding similarity.
4. **A cross-encoder re-ranker** over the union of both lists.

## Decision

Reciprocal Rank Fusion with **k = 60**, gathering more candidates per retriever than the
caller asked to receive, and keeping each retriever's contribution on the fused hit.

**k = 60** is the constant from the original RRF work, and — decisively for this project —
the constant Azure AI Search uses for its own native fusion. Matching it means the ordering
this platform produces locally and the ordering Azure produces natively are directly
comparable, which is the entire point of maintaining two backends behind one port.

**Depth before fusion.** Each retriever returns `candidates_per_retriever` hits, well above
`top_k`. A chunk ranked eighth by one retriever and unseen by the other can finish first once
fused; taking only the top few from each would discard it before fusion had a chance.

**Contributions are preserved.** Each fused hit carries which retrievers found it and at what
rank. "Only lexical found this, at rank seven" is the most useful single fact about a
surprising result, and it cannot be reconstructed after the scores are summed.

**Weights are a parameter, not a constant.** Whether lexical should outweigh dense on this
corpus is an evaluation question. It is exposed so Phase 6 can answer it with numbers.

Where the store fuses natively, that path is used instead: Azure AI Search fuses with
information the application layer does not have, and its results are adapted into the same
shape so no caller branches on which backend answered.

## Consequences

### Positive

- No retriever can dominate through the magnitude of its scores.
- Deterministic: ties break by best contributing rank, then by chunk id.
- Comparable to Azure's native fusion by construction.
- Fused results explain themselves.

### Negative

- Rank fusion discards magnitude entirely, including a genuinely informative gap between the
  first and second hit. Accepted: that information is not comparable across retrievers, and
  a re-ranker is the right place to recover it.
- Two full retrievals per query instead of one. They are independent and could be issued
  concurrently; they are not yet, because neither has been measured and the sequential
  version is simpler to reason about. Revisit with the Phase 6 latency numbers.
- One more constant to justify. Recorded here, which is the justification.

### Neutral

- The fused score has no interpretable scale. It is an ordering, not a confidence, and
  nothing should present it as one.

## Alternatives in detail

### Option 2 — Normalized score fusion

Attractive because it keeps magnitude. Rejected because normalization is computed over a
result set, so the top hit of a list of near-misses normalizes to exactly the same value as
the top hit of a list of perfect matches. It converts an incomparability into a number that
looks comparable, which is worse than admitting it.

### Option 3 — Cascade

Cheap, and a reasonable design when one retriever is clearly stronger. Rejected because it
inherits the first retriever's recall: anything lexical search misses cannot be recovered by
re-ranking what it returned, and exact-token recall is precisely what dense retrieval is bad
at. It also makes the two backends structurally different from each other.

### Option 4 — Cross-encoder re-ranking

The strongest option for quality, and the natural successor to this decision. Deferred, not
rejected: it adds a model, a latency budget and a cost per query, none of which can be
justified before the evaluation set exists to show what it buys. Azure's semantic ranker is
the same idea, and is exposed as a capability of that backend rather than assumed of both.
