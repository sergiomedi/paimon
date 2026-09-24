# Evaluating Paimon

A retrieval-augmented system is easy to change and hard to judge by eye. A chunk size, an
overlap, a fusion constant, a prompt — each can be argued about indefinitely, and each can be
settled in an afternoon against a golden set. This is how that is done here, and how to read
what comes back.

One idea runs through all of it: **verify everything a lookup can settle, and judge only what
is left.** Citations carry their offsets ([ADR-0013](adr/0013-anchor-ground-truth-to-quotations.md)),
so "did this citation point at the text it claims" is a file read, not an opinion
([ADR-0030](adr/0030-verify-attribution-before-judging-anything.md)). What survives that needs a
judgement, and a judgement is reported as one — in its own section, with the judging model
named, and marked uncalibrated until a person has checked it.

The corpus, the golden sets and how ground truth is anchored are described in
[`evaluation/README.md`](../evaluation/README.md), next to the data itself.

---

## The two benchmarks

| Run | What it measures | What it costs |
|---|---|---|
| **Retrieval** (default) | Did the retriever find the passages that answer the question — `recall@k`, `precision@k`, MRR, nDCG. | One embedding call per question. |
| **Answering** (`--answers`) | What the whole pipeline produced, and whether every citation in it survives being opened. | A generation per question, plus three judgements each when the judge is on. |

Retrieval is the one to run while tuning; it is fast, and most changes that matter are visible
in it. Answering is the one to run before believing anything, because retrieval can be perfect
and the answer still wrong.

## Running it

```bash
cd backend

# Retrieval, against the sample corpus.
uv run python -m paimon.interfaces.cli.evaluate \
    --corpus ../evaluation/corpus/sample \
    --dataset ../evaluation/datasets/retrieval-v1.jsonl \
    --label "chunk=512 overlap=64 rrf=60"

# Answering: every citation followed into the corpus.
uv run python -m paimon.interfaces.cli.evaluate --answers \
    --corpus ../evaluation/corpus/sample \
    --dataset ../evaluation/datasets/retrieval-v1.jsonl \
    --label "local ollama"
```

`--label` is not decoration. A metric without the configuration that produced it cannot be
compared with anything, which is the only thing a benchmark is for.

`--corpus` re-ingests before measuring, and on an unchanged corpus that costs nothing: the
content hash means unchanged documents are not re-embedded.

| Flag | What it does |
|---|---|
| `--corpus` | Directory to ingest first. Required with `--answers`. |
| `--dataset` | The golden set. Required. |
| `--label` | What configuration is being measured. |
| `--cutoff` | The *k* retrieval metrics are measured at. Default 8. |
| `--report` | Write the full report as JSON, including per-question scores. |
| `--against` | An earlier report to compare against, question by question. |
| `--answers` | Benchmark the answers rather than the retrieval. |
| `--write-labels` | Write a blank labelling template for this run and stop. |
| `--labels` | A filled-in template, to calibrate the judge against a person. |
| `--tenant` | Tenant to ingest and query as. Default `benchmark`. |

A long run prints its progress to **stderr**, so the report on stdout stays pipeable:

```
answering 7/15  q007
```

## Reading an answering report

```
dataset       retrieval-v1  (15 answers, 5 independent groups)
configuration local ollama

  metric                 mean +/- 95% CI      what it says
  grounded                 100.0% ± 0.0%   answers that cited anything at all
  citation accuracy        100.0% ± 0.0%   citations that survived being followed
  cited sentences           78.3% ± 31.9%  sentences carrying a marker
  fully attributed          60.0% ± 58.2%  every citation resolved, every sentence cited

  Verified, not judged: each citation above was opened at its offsets
  and checked against the text it names.

  judged by llama3.1:8b

  faithfulness              0.94 ± 0.08    stayed inside the sources it was shown
  completeness              0.87 ± 0.14    carried what the golden passages say
  relevance                 1.00 ± 0.00    addressed the question asked
```

The two blocks are different kinds of number and the report never mixes them.

**Above the line is verified.** Each of those was computed by opening a document at the offsets
a citation named and comparing the text. `citation accuracy` below 100% means the platform is
doing the exact thing it exists not to do — sounding right about something a reader cannot
check — and the run lists every offending citation underneath.

`grounded` is not a quality score. "The sources do not cover this" is the correct answer to some
questions, and an ungrounded answer is reported beside the others rather than folded into them.

**Below the line is a model's opinion of a model's output.** On the closest measured comparison
— TREC 2024 RAG, GPT-4o against human assessors on whether a passage supports a claim — a judge
of this kind agreed **56%** of the time and erred towards *over*-crediting support. The section
prints that caveat every time, along with the judging model's name, because two runs judged by
different models are not comparable and a header is the first thing lost when numbers are copied
into a table.

## The three judged questions, and what each is graded against

This is the part that is easy to get wrong, and this project got it wrong for a phase
([ADR-0033](adr/0033-faithfulness-is-graded-against-the-sources-shown.md)):

| Rubric | Graded against | A bad score means |
|---|---|---|
| `faithfulness` | the numbered sources the model was shown | it said something its sources do not carry |
| `completeness` | the passages the golden set names | it left out what those passages answer |
| `relevance` | nothing but the question | it answered something else |

Faithfulness is **precision** and completeness is **recall**, and neither is readable alone: an
answer that invents nothing *by saying nothing* scores 1.0 on the first. Relevance is shown no
passages at all, because handing them over would let a judge reward an answer for matching them
rather than for answering.

The original design graded faithfulness against the golden passages too. Those are one quoted
sentence per question, written to score retrieval — so an answer that correctly elaborated from
the rest of the same document scored the same as one that made something up. A system with zero
fabrications in fifteen answers came back at 0.667. Elsewhere the distinction is standard: Ragas
scores faithfulness against the retrieved context, and Azure AI Foundry separates *groundedness*
(precision, against context) from *response completeness* (recall, against ground truth).

Turning the judge on:

```bash
PAIMON_EVALUATION__JUDGE__ENABLED=true
# A DIFFERENT model from PAIMON_CHAT__MODEL. Startup refuses the same one:
# models score their own family more generously, by an unknown amount.
PAIMON_EVALUATION__JUDGE__MODEL=llama3.1:8b
```

Three labels rather than a score out of ten. Reasoning written before the verdict, in that order
in the JSON, so the label follows the argument instead of justifying it. An unreadable reply
becomes **undecided** and is excluded from the mean rather than counted as a failure — "the
judge broke" and "the answer was bad" are different facts — and undecided cases are reported,
because a judge abstaining on half a dataset invalidates the rest.

## Calibrating the judge against yourself

Everything above narrows how the judge can go wrong. None of it makes it agree with **you**, on
**your** corpus. Until somebody checks, the report prints `UNCALIBRATED` and says the numbers are
a figure rather than a measurement ([ADR-0032](adr/0032-a-judge-is-uncalibrated-until-a-person-checks-it.md)).

```bash
# 1. Write a blank template of this run's answers. The judge is not asked:
#    the template is blank on purpose, so its verdicts would be discarded.
uv run python -m paimon.interfaces.cli.evaluate --answers \
    --corpus ../evaluation/corpus/sample \
    --dataset ../evaluation/datasets/retrieval-v1.jsonl \
    --write-labels ../evaluation/labels/answers-v1.jsonl

# 2. Fill in the three verdicts on each line: yes / partial / no.

# 3. Hand it back.
PAIMON_EVALUATION__JUDGE__ENABLED=true \
uv run python -m paimon.interfaces.cli.evaluate --answers \
    --corpus ../evaluation/corpus/sample \
    --dataset ../evaluation/datasets/retrieval-v1.jsonl \
    --labels ../evaluation/labels/answers-v1.jsonl
```

Each line carries **both** sets of evidence — `sources` for faithfulness, `expected_passages`
for completeness — because the rubrics are graded against different material. Label one rubric
all the way down the file, then start again with the next: holding three rubrics in mind at once
is how a labeller drifts, and a blank verdict is skipped for its own metric, so working that way
is supported. The template never shows the judge's verdict; seeing it would anchor you, and an
anchored second opinion is an expensive way to confirm the first.

What comes back is **Cohen's kappa**, per rubric:

```
  calibrated against 15 reference labels
    faithfulness  kappa +0.45 (TOO LOW), raw 73.3% ± 25.3%, 15 compared, 0 skipped
    completeness  kappa +0.81 (acceptable), raw 93.3% ± 13.1%, 15 compared, 0 skipped
    relevance     kappa +1.00 (acceptable), raw 100.0% ± 0.0%, 15 compared, 0 skipped
```

Those fifteen labels were **not labelled by a person: the project owner confirms he did not label them; origin unrecorded, most likely an AI assistant in an earlier session**. Nothing in the repository or its history records who
produced them, git authorship settles nothing here, and the development journal's claim that
the owner labelled them was written by an assistant and is false.

**So by ADR-0032's own rule, no judge in this project is calibrated against a person today.**
The κ figures above, and every refusal-judge figure in Phase 9, compare one rater against
another where neither is known to be human. See
[`evaluation/labels/README.md`](../evaluation/labels/README.md) and the erratum at the top of
[ADR-0032](adr/0032-a-judge-is-uncalibrated-until-a-person-checks-it.md).

```text
```

Kappa rather than raw agreement, because raw agreement flatters any rater on a skewed dataset:
where nine answers in ten are faithful, a judge that says "yes" to everything agrees 90% of the
time and has measured nothing. Kappa discounts chance; that judge scores zero.

Below **0.6** the report names the rubrics that failed, and only those — a judge is routinely
trustworthy at one of these and not another, and an averaged figure would send the next person to
fix the wrong rubric.

**The honest limit.** Fifteen to twenty labels is far below the 200–500 per rubric that
practitioners recommend, so this catches a badly miscalibrated judge and cannot certify a good
one. Calibration also decays as models and prompts change, so a labelled set is not something
built once.

## Comparing two runs

Two configurations are compared **question by question**, never aggregate against aggregate.
Both answered the same questions, and on fifteen of them that pairing is most of the information
available ([ADR-0029](adr/0029-benchmark-numbers-carry-their-uncertainty.md)):

```bash
uv run python -m paimon.interfaces.cli.evaluate --dataset ... \
    --label "chunk=512" --report ../evaluation/reports/512.json

uv run python -m paimon.interfaces.cli.evaluate --dataset ... \
    --label "chunk=256" --against ../evaluation/reports/512.json
```

Every metric comes back with its difference, a confidence interval and a verdict:
*distinguishable from zero*, or *not distinguishable from noise*. That is what accepting or
rejecting a retrieval change means here.

Standard errors are **clustered by source document**: questions about one runbook share its
wording and whatever the chunker made of it, so counting them as independent overstates
confidence. **The intervals are wide, and that is the finding** — this dataset cannot settle
small differences, and a wide interval printed honestly is the thing that says so.

## When the run refuses to start

Some flag combinations cannot mean anything, and each of these was once accepted and quietly
ignored — which is worse, because the result is a complete and plausible report of the wrong
thing. They now stop the run with exit code **2** and an explanation:

| Command line | Why |
|---|---|
| `--answers` without `--corpus` | Citations are verified by opening documents; without a corpus every one is reported as pointing at an unknown document, and the run prints 0.0% accuracy for a system that may be perfect. |
| `--labels` with the judge off | Calibration compares a person's verdicts with the judge's, and there are none. |
| `--labels` with `--write-labels` | One writes a blank template, the other reads a filled one. |
| `--labels` or `--write-labels` without `--answers` | Labelling is about answers; a retrieval run has none. |
| `--answers` with `--against` | Paired comparison of two answering runs is not wired to this command yet. |

Exit code 1 is different, and means the benchmark ran and scored nothing.

## What this cannot tell you

- **The sample corpus is written material**, and therefore cleaner and more uniform than real
  documentation. Numbers from it say the pipeline works; they do not predict how it behaves on a
  real corpus.
- **A high faithfulness score is not a statement that the answer is true.** It says the answer
  stayed inside what it was shown. An answer that faithfully repeats a passage that should never
  have been retrieved scores well here, and badly in the retrieval metrics, which is where that
  failure belongs.
- **Whether a *particular* marker is the right source for the sentence carrying it** is not
  judged. Citation resolution is verified; claim-to-marker attribution is a rubric that does not
  exist yet.
- **Latency** here is measured against whatever model is configured, on one machine, with no
  concurrency. It is a smoke signal, not a performance benchmark.

## Where it lives

| Path | What it holds |
|---|---|
| `backend/src/paimon/evaluation/` | The benchmark itself: metrics, statistics, attribution, the judge, calibration. Pure with respect to infrastructure. |
| `backend/src/paimon/evaluation/statistics.py` | Estimates, clustered standard errors and paired differences, implemented rather than imported. |
| `backend/src/paimon/evaluation/attribution.py` | Following a citation into the corpus. |
| `backend/src/paimon/evaluation/judging.py` | The three rubrics, and what each is graded against. |
| `backend/src/paimon/evaluation/calibration.py` | Cohen's kappa, and the labelling file. |
| `backend/src/paimon/interfaces/cli/evaluate.py` | The command line: wiring, rendering, and the refusals above. |
| `evaluation/` | Corpus, golden sets, labels, reports. |


## Evaluating an agent

An agent decides how many steps to take, so two runs of the same task differ, and it can
refuse, so "was it right" is no longer the only question. The rules are
[ADR-0046](adr/0046-how-an-agent-is-evaluated.md); this is how to run it.

```bash
# Four systems over the agent task set, k attempts each.
uv run python -m paimon.interfaces.cli.evaluate --agents \
    --agent investigator \
    --dataset ../evaluation/datasets/agents-v1.jsonl \
    --corpus ../evaluation/corpus/sample \
    --tenant benchmark --trials 5 \
    --report ../evaluation/reports/agents-investigator.json \
    --journal ../evaluation/reports/agents-investigator-journal.jsonl

# Compare two systems task by task.
uv run python -m paimon.interfaces.cli.evaluate --agents --agent incident-triage \
    ... --against ../evaluation/reports/agents-investigator.json

# Re-score existing transcripts without re-running anything.
uv run python -m paimon.interfaces.cli.evaluate --agents --agent investigator \
    --dataset ... --corpus ... --regrade <report> --report <graded>
```

`--agent` takes any registered agent, `answers` for the single-pass path, or `oracle` to
check the graders can be satisfied at all. `--journal` records each attempt as it finishes
and resumes from it, which matters: a three-hour run that keeps nothing until the end loses
everything to a machine that runs out of memory.

### What is scored, and what is only reported

**Scored:** the outcome. An attempt passes when it is right *and* checkable — correct
outcome, full support coverage, citation precision 1.0.

**Reported, never scored:** tool calls, repeated calls, tool errors, stop reason, tokens,
latency, and each step's own details. Call order is not scored at all: an agent's claim is
that the route is not fixed, so grading the route tests the dataset author's imagination.

**pass^k is the number to read.** The share of tasks a system gets right on *every* attempt.
A system that needs three tries is not one an operator can rely on, and pass@1 hides that.

### The refusal judge

One binary question — did this text answer or decline — shown the response and **nothing
else**, not the question. Shown the question it grades completeness instead, which broke 19
correct answers while fixing 25 when it was tried.

It is calibrated before its numbers are used, against a threshold written down first. For
the Azure window: κ ≥ 0.80 and a bootstrap CI lower bound ≥ 0.65; phi4 returned κ = 0.892,
CI [0.748, 1.000]. Wherever those numbers appear they carry their provenance: **calibrated
against a reference labelled by Claude (Opus), not blind to category; kappa is an upper
bound.**

The out-of-corpus cell is **read in full rather than sampled** — at most 60 attempts, and
the only place the expensive error can occur. That column is reported as *labelled*, not
judged. It matters: code grading scored `answers` at 0% there, and reading all 60 found it
had refused 15 of 15 correctly.

### Running the benchmark safely

Integration tests truncate tables, and twice they have destroyed measurement data. The
fixture now refuses any database whose name does not end in `_test`, and `scripts/check.sh`
creates and uses `paimon_test`. Reports carry their own evidence — including the text a
`verify` node withdrew — so re-running the suite cannot empty the record of an experiment.
