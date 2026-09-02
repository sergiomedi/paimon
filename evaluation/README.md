# Evaluation

Retrieval is the part of this platform that is easiest to change and hardest to
judge by eye. A chunk size, an overlap, a fusion weight, a dimensionality — each
of them can be argued about indefinitely and settled in an afternoon with a
dataset. This directory is that dataset.

## What is here

| Path | What it is |
|---|---|
| `corpus/sample/` | A small operational corpus, written for this repository, so the benchmark runs immediately after a clone. |
| `corpus/manifest.json` | Public corpora to evaluate against properly, with their licences. |
| `datasets/*.jsonl` | Golden sets: questions and the passages that answer them. |
| `reports/` | Benchmark output. Git-ignored; runs are cheap and results are not a source of truth. |

## Why two corpora

The sample corpus is written material and therefore cleaner and more uniform than
real documentation. Numbers from it say the pipeline works; they do not predict
how it will behave on a real corpus, and this file exists partly so nobody quotes
them as if they did.

The manifest points at public runbooks, postmortems and architecture decision
records under licences that permit it. Those are what the reported benchmark uses.
They are fetched rather than vendored: redistributing them would mean taking on
their licence terms, and a corpus in git is a corpus that goes stale.

## Ground truth is anchored to quotations, not chunks

Each case names a document and quotes the passage that answers the question. A
retrieval counts as successful when a returned chunk comes from that document and
contains the quotation.

It would be simpler to record chunk ids. It would also make the ground truth a
function of the chunking policy — so the moment anyone changed the chunk size,
every case would have to be rewritten, and the one experiment the benchmark exists
to run would be the one thing it could not measure. See
[ADR-0013](../docs/adr/0013-anchor-ground-truth-to-quotations.md).

## Running it

```bash
cd backend
uv run python -m paimon.interfaces.cli.evaluate \
    --corpus ../evaluation/corpus/sample \
    --dataset ../evaluation/datasets/retrieval-v1.jsonl \
    --label "chunk=512 overlap=64 rrf=60"
```

The label is not decoration. A metric without the configuration that produced it
cannot be compared with anything, which is the only thing a benchmark is for.
