# ADR-0046: How an agent is evaluated

- **Status:** Accepted
- **Date:** 2026-09-24
- **Deciders:** Sergio Medina
- **Phase:** 9 — Autonomy, and whether it pays for itself

## Context and problem statement

[ADR-0013](0013-a-golden-set-with-anchored-citations.md) and
[ADR-0029](0029-paired-comparisons-over-aggregate-scores.md) settled how retrieval and
answering are measured. An agent breaks both assumptions: it decides how many steps to take,
so two runs of the same task differ; and it can refuse, so "did it answer correctly" is no
longer the only question.

The failure mode to avoid is specific. An agent benchmark that scores the **route** rewards
an agent for taking the steps its author imagined, which is a test of the author's
imagination. One that scores a single attempt reports a sample of size one from a
distribution nobody characterised.

## Decision drivers

- Autonomy's whole claim is that the route is not fixed. Scoring it contradicts the claim.
- A system that is right four times in five is different from one right once in five, and
  an average hides which.
- Refusal is a correct outcome on some tasks and a failure on others, and no code can tell
  the difference from the text alone.
- Cost is the thing autonomy is being weighed against, so it is reported, never scored.

## Considered options

1. Score the trajectory against a reference trajectory.
2. Score one attempt per task on the final outcome.
3. **k attempts per task, scored on the outcome; trajectory reported, never scored.**

## Decision

**Option 3.**

**k attempts per task**, 5 locally and 3 on Azure, reported as pass@1, pass@k and **pass^k** —
the share of tasks a system gets right on *every* attempt. pass^k is the number an operator
cares about, because a system that needs three tries is not a system they can rely on.

**Graded by code wherever code can decide.** Whether a cited passage exists, whether it is
the one the task anchors on, whether the answer's markers resolve — all mechanical, all
checked by opening the document at the offsets claimed. An answer passes only when it is
right **and** checkable: correct outcome, full support coverage, citation precision 1.0.

**A model judges one thing only: did this text answer the question or decline it.** Binary,
shown the response and nothing else — not the question. Shown the question it grades
completeness, which broke 19 correct answers while fixing 25 when it was tried.

**Call order is not scored. Trajectory is reported, never scored**: tool calls, repeated
calls, tool errors, stop reason, tokens, latency, and — since this phase — each step's own
details, so the report holds what a run recorded rather than only what it answered.

**Paired comparisons**, task by task (ADR-0029), because the systems agree about which tasks
are hard and that correlation is most of the information a set this size has.

**The judge is calibrated before its numbers are used, against a rule fixed in writing
beforehand.** For the Azure window: κ ≥ 0.80 **and** a bootstrap CI lower bound ≥ 0.65.
phi4 returned κ = 0.892, CI [0.748, 1.000], raw agreement 95.6% over 68 texts — it passes.

**The judge is described honestly wherever it appears:** *calibrated against a reference
labelled by Claude (Opus), not blind to category; kappa is an upper bound.*

**One cell is read in full rather than sampled.** The error that matters — an answer to a
question the corpus cannot support, recorded as a refusal — can only occur in the
out-of-corpus cell, and that cell is at most 60 attempts. Every one is read, so no verdict
there is the judge's unless the labeller agreed. The column is reported as **labelled**, not
judged, which is a stronger claim than any κ over a sample supports.

## Consequences

### Positive

- The comparison survives a model that takes a different route on every run.
- pass^k made a real difference visible that pass@1 hid.
- Reading the out-of-corpus cell in full overturned it: code grading scored `answers` 0%
  there, and the audit found it refused **15 of 15** correctly. The 0% was the grader failing
  to recognise a prose refusal, not the system fabricating.

### Negative

- k attempts cost k times as much. 450 local runs took 3h12m.
- Thresholds fixed in advance can be wrong in advance. The first refusal rule demanded a CI
  lower bound > 0.80, which at n=39 with 7 positives admitted **only a perfect run**. It was
  replaced — before any data existed for the run it governed — and the replacement is
  recorded beside it rather than in place of it.
- The judge is a language model calibrated against a reference labelled by another language
  model. κ measures whether two raters agree, not whether either is right.

### Neutral

- Trajectory counters are `None`, not zero, for systems that have no such notion. Reporting
  `0.000 ± 0.000` tool calls for a fixed graph states a measurement of something it does not do.

## What this phase learned the hard way

- **Never run a benchmark against a database a test may truncate.** Twice integration tests
  destroyed measurement data. The answer is not another table guard: the fixture now refuses
  any database whose name does not end in `_test`, and reports carry their own evidence.
- **A pre-registered sample can fail to fill its cells, and that is reported, not backfilled.**
  The Azure sample drew 71 of a targeted 80 because the content filter and a canned refusal
  emptied three cells. Reaching 80 by taking extra one-hop texts would have changed the
  stratification quietly.
- **Exclusion needs evidence per attempt.** An agent failure that records no cause is not
  assumed to be a provider refusal; the proof is recovered from the run log, and where it
  cannot be, the failure counts against the system.
