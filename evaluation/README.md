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

## Every number carries its interval

Fifteen questions produce averages that move by several points on nothing at all. So the
benchmark reports `73.3% ± 21.4%`, not `73.3%`, and the standard errors are **clustered by
source document**: questions about one runbook share its wording and whatever the chunker made
of it, so counting them as independent observations overstates confidence.

Two configurations are compared **question by question**, not aggregate against aggregate. Both
answered the same questions, and that fact is most of the information available at this size:

```bash
cd backend
uv run python -m paimon.interfaces.cli.evaluate \
    --dataset ../evaluation/datasets/retrieval-v1.jsonl \
    --label "chunk=512" --report ../evaluation/reports/512.json

uv run python -m paimon.interfaces.cli.evaluate \
    --dataset ../evaluation/datasets/retrieval-v1.jsonl \
    --label "chunk=256" --against ../evaluation/reports/512.json
```

The second run prints the difference for every metric with its confidence interval and a
verdict: *distinguishable from zero*, or *not distinguishable from noise*. That is what
accepting or rejecting a retrieval change means here. See
[ADR-0029](../docs/adr/0029-benchmark-numbers-carry-their-uncertainty.md).

**The intervals are wide, and that is the finding.** This dataset cannot settle small
differences. It needs to grow before it can, and a wide interval printed honestly is the thing
that says so.

## Answers are verified, not judged

`--answers` runs the answering use case over the same golden set and **follows every citation
into the corpus**: open the document, go to the offsets, check the quoted text is there.

```bash
cd backend
uv run python -m paimon.interfaces.cli.evaluate --answers \
    --corpus ../evaluation/corpus/sample \
    --dataset ../evaluation/datasets/retrieval-v1.jsonl \
    --label "local ollama"
```

No model grades this. That is not thrift, it is accuracy: the TREC 2024 RAG track measured
GPT-4o against human assessors on exactly this question — does a passage support a claim — and
they agreed **56%** of the time, with the model systematically **over-crediting** support. A
judge that errs towards "yes, that was supported" is the worst instrument for catching an
invented answer, and this platform does not need one, because ADR-0013 made citations carry
their offsets. See [ADR-0030](../docs/adr/0030-verify-attribution-before-judging-anything.md).

What is verified: that each citation resolves, that each sentence carries a marker, and that no
marker refers to a source that does not exist. What is **not**: whether the sentence around a
resolving citation is a fair reading of it. That needs a judge, it arrives next, and it will be
labelled as judged wherever it appears.

## And a judge, for the two questions arithmetic cannot answer

Whether the sentence around a resolving citation is a *fair reading* of it, and whether the
answer *addresses the question*, need a judgement. So there is one, off by default, and on terms
chosen against what such judgements are measurably worth
([ADR-0031](../docs/adr/0031-a-judge-on-terms-that-account-for-what-it-is-worth.md)):

```bash
PAIMON_EVALUATION__JUDGE__ENABLED=true
# A DIFFERENT model from PAIMON_CHAT__MODEL. Startup refuses the same one.
PAIMON_EVALUATION__JUDGE__MODEL=llama3.1:8b-instruct
```

Three labels rather than a score out of ten; reasoning written before the verdict; graded against
the passage the golden set names; an unreadable reply becomes **undecided** rather than a default
label, and undecided cases are counted, not averaged away. Ties across repeated samples are
undecided too — breaking them towards "yes" would bias the aggregate in the direction judges
already err.

The judged numbers appear in their own section, with the judge's name and the 56% caveat printed
beside them. They are never mixed with the verified ones, and a run with no judge prints no
judged section at all.

## The judge is uncalibrated until you check it

Everything about the judge narrows how it can go wrong. None of it makes it agree with **you**.
So until somebody labels a sample, the judged section prints `UNCALIBRATED` and says the numbers
are a figure rather than a measurement ([ADR-0032](../docs/adr/0032-a-judge-is-uncalibrated-until-a-person-checks-it.md)).

Labelling takes one pass over a file:

```bash
cd backend
# 1. Run, and write a template of the answers to label.
uv run python -m paimon.interfaces.cli.evaluate --answers \
    --corpus ../evaluation/corpus/sample \
    --dataset ../evaluation/datasets/retrieval-v1.jsonl \
    --write-labels ../evaluation/labels/answers-v1.jsonl

# 2. Fill in the two blank verdicts on each line: yes / partial / no.
#    Leave a line blank to skip it — a half-finished file still works.

# 3. Run again with the labels.
uv run python -m paimon.interfaces.cli.evaluate --answers \
    --dataset ../evaluation/datasets/retrieval-v1.jsonl \
    --labels ../evaluation/labels/answers-v1.jsonl
```

The template deliberately **does not show you what the judge decided**. Seeing it would anchor
you, and an anchored second opinion is an expensive way to confirm the first.

What comes back is **Cohen's kappa**, not raw agreement. Raw agreement flatters any rater on a
skewed dataset: where nine answers in ten are faithful, a judge that says "yes" to everything
agrees 90% of the time and has measured nothing. Kappa discounts chance; that judge scores zero.
Below **0.6** the report says the two of you are not reliably measuring the same thing.

**The honest limit:** fifteen to twenty labels is far below the 200–500 per rubric practitioners
recommend, so this catches a badly miscalibrated judge and cannot certify a good one. And
calibration decays — judges drift within 60–90 days as models and prompts change — so a labelled
set is not something you build once.

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
