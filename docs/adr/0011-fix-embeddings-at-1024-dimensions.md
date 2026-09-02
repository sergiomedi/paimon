# ADR-0011: Fix embeddings at 1024 dimensions

- **Status:** Accepted
- **Date:** 2026-09-02
- **Phase:** 2 — RAG

## Context and problem statement

An index is built on one embedding model at one dimensionality, and changing either
means reindexing the entire corpus. The number therefore has to be chosen before the
first document is ingested, not discovered afterwards.

Two facts, checked rather than recalled, constrain the choice.

`text-embedding-3-large` is the most capable embedding model Azure sells directly, and it
emits **3072 dimensions**. It supports Matryoshka truncation through the API's `dimensions`
parameter, so a shorter vector can be requested without a second model.

pgvector 0.8.5 stores up to 16000 dimensions in a `vector` column but **indexes only up to
2000 of them with HNSW**. Above that the options are `halfvec`, which indexes to 4000 at
half precision, or no index at all — a sequential scan over every chunk in the tenant.

So the production model's native output cannot be indexed by the development backend. That
is not a detail to discover after ingesting a corpus.

## Decision drivers

- Both retrieval backends must use the same schema, or the ADR-0003 comparison is between
  two different systems rather than two implementations of one.
- The local backend must have a real index; a sequential scan is not a retrieval system.
- Storage and index build time scale linearly with dimensionality.
- Any quality cost must be measurable rather than assumed.

## Considered options

1. **1024 dimensions everywhere.** Azure requests it via the `dimensions` parameter; the
   local model produces it natively.
2. **3072 dimensions**, with `halfvec` for the local backend.
3. **1536 dimensions**, matching `text-embedding-3-small` natively.
4. **Different dimensionality per backend**, each at its model's native size.

## Decision

1024, fixed platform-wide, declared in one place and enforced by the index descriptor that
every store carries.

| Adapter | Model | How it reaches 1024 |
|---|---|---|
| Development, CI | BGE-M3 | native |
| Azure | `text-embedding-3-large` | `dimensions=1024` |

Both backends then share one schema, one `vector(1024)` column, and one HNSW index. Storage
and index memory are a third of what 3072 would cost, and the local index is a real HNSW
index rather than a scan.

The quality cost of truncating from 3072 is real but small by OpenAI's published figures,
and — this is the part that matters — it is **measurable**: the Phase 6 benchmark can run
the same corpus at several dimensionalities and report the difference, which turns this
decision from a guess into a number.

## Consequences

### Positive

- One schema for both backends, so the retrieval comparison compares retrieval.
- HNSW works on the local backend with no `halfvec` conversion and no precision loss.
- A third of the storage and index memory of 3072, which on a laptop is the difference
  between comfortable and not.
- The dimensionality is stated in one constant and asserted by the store on every write, so
  a mismatched embedding is refused rather than silently indexed.

### Negative

- Some retrieval quality is left on the table relative to full 3072-dimensional vectors.
  Accepted, and scheduled to be quantified in Phase 6 rather than argued about.
- Changing the number later means a new migration and a full reindex of the corpus. That is
  inherent to the decision, not to this particular value.
- The local model is constrained to those producing or truncating to 1024, which excludes
  some otherwise reasonable choices.

### Neutral

- The `dimensions` parameter is ignored by providers that do not support truncation; the
  adapter validates the width it actually receives, so an unsupported provider fails at the
  first call rather than writing wrong-sized vectors.

## Alternatives in detail

### Option 2 — 3072 with halfvec locally

Keeps the production model's full output. Rejected because it makes the two backends
structurally different — one indexes `vector`, the other `halfvec` at half precision — so
any measured difference between them would confound the retrieval backend with the numeric
precision. It also triples storage for a corpus that is read far more often than it is
written.

### Option 3 — 1536, matching text-embedding-3-small

Native for the smaller Azure model and comfortably indexable. Rejected because it gives up
the larger model's quality without buying anything 1024 does not already provide: both are
under the 2000-dimension ceiling, and 1536 costs half again as much storage.

### Option 4 — Different dimensionality per backend

Each model at its best size. Rejected outright: it means two schemas, two migrations, and a
comparison in Phase 6 that cannot attribute a difference to anything in particular. The
whole value of the ports in ADR-0003 is that the two adapters are interchangeable, and they
are not interchangeable if their indexes have different shapes.
