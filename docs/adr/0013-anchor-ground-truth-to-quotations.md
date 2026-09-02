# ADR-0013: Anchor evaluation ground truth to quotations, not chunk ids

- **Status:** Accepted
- **Date:** 2026-09-02
- **Phase:** 2 — RAG

## Context and problem statement

The retrieval benchmark needs to know, for each question, what a correct retrieval looks
like. The obvious representation is the identifiers of the chunks that should come back:
they are exact, they are cheap to compare, and the retriever returns them directly.

The obvious representation is also the wrong one, and the reason is worth stating precisely.
A chunk id is a function of the chunking policy. `runbook:7` means the eighth span produced
by a particular maximum size, overlap and splitting strategy. Change the chunk size from 512
to 384 tokens and every id in the corpus changes.

The benchmark exists, more than anything else, to answer questions of the form *is 384 better
than 512*. A ground truth expressed in chunk ids makes that the one experiment it cannot run:
every case would have to be rewritten by hand for each configuration, and any comparison
would be between a dataset and a differently-labelled copy of itself.

## Decision drivers

- Ground truth must survive a change to any parameter the benchmark measures.
- Judging must be mechanical; a human in the loop per run means the benchmark runs rarely.
- Cases must be readable and reviewable in a diff, since the dataset is what every number is
  relative to.
- Writing a case must be cheap enough that the set grows.

## Considered options

1. **Document plus quotation.** A case names a document and quotes the passage that answers
   the question; a retrieval succeeds when a returned chunk from that document contains the
   quotation.
2. **Chunk ids.**
3. **Document ids alone.**
4. **Character spans** into the document's normalized text.
5. **Model-judged relevance**, scoring each retrieved chunk with an LLM.

## Decision

Option 1, with whitespace- and case-insensitive matching.

A case records `document_id` and `quote`. Matching normalizes whitespace before comparing,
because chunk boundaries and Markdown reflowing change line breaks without changing words,
and ground truth that breaks when a paragraph is rewrapped is ground truth nobody maintains.

The dataset is JSON Lines, one case per line, so a diff shows exactly which questions changed.
A case with no supporting passage is rejected at load: it can be neither right nor wrong, so
it would score as a failure against every configuration and drag every average down while
looking like a signal.

## Consequences

### Positive

- Chunk size, overlap, splitting strategy, embedding model, dimensionality and fusion weights
  can all be varied without touching the dataset. That is the entire point.
- Cases are readable: a reviewer can see the question and the sentence that answers it.
- Judging is exact and free, so the benchmark can run on every change.
- The same dataset scores the pgvector and Azure AI Search backends, which is what makes the
  ADR-0003 comparison meaningful.

### Negative

- A quotation can go stale if the corpus is edited. Real, and mitigated by loading failing
  loudly: a case whose quote no longer appears anywhere scores zero and shows up in the
  failure list rather than passing silently.
- Choosing the quotation is a judgement call, and a badly chosen one — too long, or too
  incidental — measures the wrong thing. Mitigated by review, and by keeping quotes to the
  sentence that actually answers the question.
- Recall is measured against passages the author thought of. A retriever that finds a better
  supporting passage scores no higher for it. Accepted: the alternative is model-judged
  relevance, below.

### Neutral

- Precision is measured against the cutoff rather than against the number of chunks returned,
  so a configuration that retrieves fewer chunks is not scored more leniently.

## Alternatives in detail

### Option 2 — Chunk ids

Exact and trivial to compare, and correct for a system whose chunking never changes. Rejected
for the reason this ADR exists: it couples the ground truth to the variable under test.

### Option 3 — Document ids alone

Survives every configuration change, and is far too coarse. A corpus of five documents makes
a random retriever look competent, and the metric cannot distinguish returning the right
paragraph from returning the right file.

### Option 4 — Character spans

Precise, and stable across chunking changes since offsets index the document rather than the
chunk. Rejected on maintenance: an offset pair is unreadable in a diff and silently wrong
after any edit to the document, whereas a quotation is self-describing and its staleness is
detectable.

### Option 5 — Model-judged relevance

Scales to a large corpus and rewards genuinely good retrieval the author did not anticipate.
Deferred rather than rejected: it costs a model call per retrieved chunk per run, it is
non-deterministic, and — decisively — a benchmark judged by a model cannot be trusted to
evaluate a change to the model. It belongs in Phase 6 as a second opinion alongside these
numbers, not as the numbers themselves.
