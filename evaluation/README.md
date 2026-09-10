# Evaluation data

Retrieval is the part of this platform that is easiest to change and hardest to judge by eye. A
chunk size, an overlap, a fusion weight, a dimensionality — each of them can be argued about
indefinitely and settled in an afternoon with a dataset. This directory is that dataset.

**How to run the benchmark and read what comes back is in
[`docs/evaluation.md`](../docs/evaluation.md).** This file is about the material it runs against.

## What is here

| Path | What it is |
|---|---|
| `corpus/sample/` | A small operational corpus, written for this repository, so the benchmark runs immediately after a clone. |
| `corpus/manifest.json` | Public corpora to evaluate against properly, with their licences. |
| `datasets/*.jsonl` | Golden sets: questions and the passages that answer them. |
| `labels/` | A person's verdicts on a sample of answers, used to calibrate the judge. See [`labels/README.md`](labels/README.md). |
| `reports/` | Benchmark output. Git-ignored; runs are cheap and results are not a source of truth. |

## Why two corpora

The sample corpus is written material and therefore cleaner and more uniform than real
documentation. Numbers from it say the pipeline works; they do not predict how it will behave on
a real corpus, and this file exists partly so nobody quotes them as if they did.

The manifest points at public runbooks, postmortems and architecture decision records under
licences that permit it. Those are what the reported benchmark uses. They are fetched rather than
vendored: redistributing them would mean taking on their licence terms, and a corpus in git is a
corpus that goes stale.

## Ground truth is anchored to quotations, not chunks

Each case names a document and quotes the passage that answers the question. A retrieval counts
as successful when a returned chunk comes from that document and contains the quotation.

It would be simpler to record chunk ids. It would also make the ground truth a function of the
chunking policy — so the moment anyone changed the chunk size, every case would have to be
rewritten, and the one experiment the benchmark exists to run would be the one thing it could not
measure. See [ADR-0013](../docs/adr/0013-anchor-ground-truth-to-quotations.md).

**A quoted passage is a minimal anchor, not a description of what the corpus supports.** It names
the sentence that answers the question, chosen so ground truth survives a change of chunk size.
That makes it the right reference for `recall@k` and for judging whether an answer *carries* what
it says, and the wrong one for judging whether an answer stayed inside its evidence — a mistake
this project made and documented in
[ADR-0033](../docs/adr/0033-faithfulness-is-graded-against-the-sources-shown.md).

## Fifteen questions is a small dataset, and the numbers say so

Averages over fifteen cases move by several points on nothing at all, so every number is reported
as `73.3% ± 21.4%` rather than `73.3%`, with standard errors **clustered by source document**:
questions about one runbook share its wording and whatever the chunker made of it, so counting
them as independent observations overstates confidence
([ADR-0029](../docs/adr/0029-benchmark-numbers-carry-their-uncertainty.md)).

**The intervals are wide, and that is the finding.** This dataset cannot settle small
differences. It needs to grow before it can, and a wide interval printed honestly is the thing
that says so.

## Adding a case

One JSON object per line, so a diff shows exactly which questions changed — which matters when
the dataset is the thing every measurement is relative to:

```json
{"id": "q016", "question": "...", "supporting": [{"document_id": "oncall-handbook", "quote": "Acknowledge within five minutes."}], "tags": ["procedure"]}
```

The quote must appear **verbatim** in the parsed text of that document, and loading fails loudly
if it does not: a benchmark that silently skips an unscoreable case reports a better number for
having fewer hard questions in it.
