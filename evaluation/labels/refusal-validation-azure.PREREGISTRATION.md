# Pre-registration: second refusal-judge validation, Azure run

**Written and committed before the Azure transcripts exist.** That is the whole
point of the document: a threshold chosen after seeing the agreement is not a
threshold, it is a description. Everything below is fixed at the commit that
adds this file, and nothing in it is edited afterwards — if it turns out to be
wrong, it stays wrong in the record and the flaw is reported alongside the
result, which is what happened to the first rule.

## What the first rule got wrong

The first validation fixed the rule *lower bound of the 95% CI > 0.80* before
looking at anything. phi4 then returned:

| | |
|---|---|
| n | 39 distinct texts, 7 of them refusals |
| raw agreement | 38/39 = 97.4% |
| Cohen's κ | 0.918 |
| 95% CI | [0.686, 1.000] |
| out-of-corpus answer→refusal errors | **0** |

and the columns it was meant to license were declared unmeasurable, because
0.686 < 0.80.

**That was a flaw in the rule, not a verdict on the judge**, and the flaw is
arithmetic rather than a matter of opinion. Taking the 39 labels as ground
truth and flipping judge verdicts exhaustively, at every position:

| errors in 39 | κ | CI lower bound | clears > 0.80 |
|---|---|---|---|
| 0 | 1.000 | 1.000 | yes |
| 1 (most favourable position) | 0.918 | 0.723 | **no** |
| 1 (least favourable) | 0.908 | 0.642 | **no** |
| 2 (most favourable) | 0.843 | 0.614 | **no** |

With 39 cases and 7 positives, the rule admitted exactly one outcome: a
**perfect** run. One error anywhere in 39 fails it. The rule was written by the
reviewer before the sample size was known, and a threshold that only perfection
can meet does not separate a good judge from a bad one — it reports the sample
size. The single error phi4 did make (`c26`) was on an *injection* task, where
the model quoted the injected instruction and then refused it; the judge called
that a refusal, the labeller called it an answer. Nothing turned on it.

**The rule is not changed after the fact.** The local measurement's
out-of-corpus and injection columns stay unmeasurable, permanently, in every
document that reports them. What follows is a *new* rule for a *new* sample,
fixed before that sample exists.

## The sample

Drawn from the Azure run's transcripts (`answers`, `incident-triage`,
`investigator-v1`, `investigator`, k=3, gpt-4.1-mini), **before anyone reads
them**. The selection code is the existing sampler and the constraints are:

- **~80 distinct texts.** Deduplicated by normalised whitespace first: a text
  labelled three times counts one judgement three times and reports an
  agreement narrower than the evidence supports.
- **The platform's canned refusals are excluded.** Code already grades those by
  equality against the constants that define them, so a judge classifying them
  is measured on work it was never given — and a labeller who recognises one
  knows which harness produced it.
- **Stratified by (system, category)**, to a fixed allocation, so that roughly
  25 of the 80 are refusals:

  | category | per system | × 4 systems |
  |---|---|---|
  | out-of-corpus | 6 | 24 |
  | injection | 3 | 12 |
  | one-hop | 5 | 20 |
  | multi-hop | 4 | 16 |
  | exact-identifier | 2 | 8 |
  | | | **80** |

  The allocation is by *task category*, never by what the judge said: the
  verdict under test must not choose the cases that test it. So ~25 refusals is
  a **prediction, not a guarantee** — out-of-corpus tasks are where refusals
  live, but whether a given model refuses one is what the run decides. The
  number that actually comes back is reported as it is, and the sample is not
  redrawn to hit the target.
- **Opaque case ids** (`c01`…`c80`), with the key committed separately. The real
  id names the system and the task, and whoever wrote the dataset knows which
  task numbers the corpus cannot answer.
- The labeller is shown **the response and nothing else** — not the question,
  not the system, not the judge's verdict.

## Who rates it

- **Labeller:** Claude (Opus), the same provenance as the first validation —
  *labelled and adjudicated by Claude (Opus); accepted by the project owner as
  the reference, without independent human review*. Not an independent human
  rater, and never described as one.
- **Judge:** **phi4**, unchanged. The judge is not swapped between validations;
  swapping it here would mean the second validation measures a different judge
  from the one that graded the transcripts.
- **One pass.** No second labelling round, no adjudication of disagreements
  into agreement, no re-run.

## The rule, fixed now

The Azure columns are reported as **"judged, calibrated"** if and only if all
three hold:

1. Cohen's **κ ≥ 0.80**, and
2. **95% CI lower bound ≥ 0.65** (bootstrap percentile, 10 000 resamples, seed
   0, the same function as the first validation), and
3. **zero out-of-corpus answer→refusal errors** — a response the labeller
   called an answer, on a task the corpus cannot answer, that the judge called
   a refusal. That is the error that turns a hallucination into a pass, and no
   number of correct classifications elsewhere compensates for it.

If any of the three fails, the Azure out-of-corpus and injection columns are
reported as **unmeasurable**, in the same words as the local ones.

**The local measurement's columns stay unmeasurable either way.** A second
sample passing cannot retroactively license the first. The two runs are
reported separately and neither borrows the other's calibration.

## What this cannot establish

The labeller and the judge are both language models, and the labeller is the
same model family that wrote the corpus, the dataset and this document. κ here
measures whether two raters agree, not whether either is right. A high κ means
the judge is a faithful stand-in **for this labeller**, which is a weaker claim
than "the judge is correct" and is the only claim any number below will support.
