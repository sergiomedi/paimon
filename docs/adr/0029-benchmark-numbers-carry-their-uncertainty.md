# ADR-0029: Benchmark numbers carry their uncertainty, and two runs are compared in pairs

- **Status:** Accepted
- **Date:** 2026-09-05
- **Phase:** 6 — Evaluation

## Context and problem statement

The README has claimed since Phase 2 that *"retrieval changes are accepted or rejected on
numbers"*. Until now the benchmark reported bare means over fifteen questions, and that claim
was not true: on a dataset this size, a mean moves several points on nothing at all, and there
was no way to tell a real improvement from the dataset having a mood.

This is not a small gap. It is the difference between a benchmark and a number generator, and
everything Phase 6 adds — judged metrics especially — rests on being able to say how sure a
number is.

## Decision drivers

- A mean without an interval invites a comparison it cannot support.
- Fifteen questions is a small sample, and the questions are not independent.
- A change worth making should be distinguishable from noise; one that is not should be
  reported as not.
- The platform should not acquire a large numerical dependency for a tail probability.

## Considered options

1. **Bootstrap** the intervals by resampling the dataset.
2. **Central limit theorem** for standard errors, clustered where the data is clustered, and a
   paired test for comparisons.
3. **Report the means and let the reader judge**, as before.

## Decision

Option 2, following Anthropic's *Adding Error Bars to Evals*. Three parts, and the third is the
one that matters.

**Standard errors from the CLT rather than bootstrapping.** Simpler, closed-form, no random
seed to make a report irreproducible, and for a mean it is what the bootstrap converges to
anyway.

**Clustered by source document.** Questions about one runbook are not independent observations:
they share its wording, its structure and whatever the chunker made of it. Treating them as
independent understates the standard error — by a factor of three or more in the literature —
and the degrees of freedom must count **clusters**, not questions. Fifteen questions across five
documents carry about as much information as five independent observations, and the interval now
says so out loud. It is a wide interval. That is the honest one.

**Comparisons are paired, question by question.** This is the part that turns the benchmark into
an instrument. Both configurations answer the same questions, so the difference is measured per
question and averaged, rather than subtracting two aggregates:

    Var(paired) = Var(unpaired) - 2*Cov(A, B)/n

Two retrieval configurations agree strongly about which questions are hard. That covariance is
large and positive, so pairing removes most of the variance — which is how a genuine three-point
improvement becomes visible on fifteen questions, and how a change that only looks like one is
caught. The correlation is reported alongside the difference, because it is what pairing
exploits: near zero and pairing bought nothing.

Comparing runs of different datasets is refused rather than approximated. Pairing scores from
different questions is not a paired comparison; it is a wrong number with a confident interval
around it.

**The t distribution is implemented here, in about forty lines.** The alternative was SciPy: tens
of megabytes of compiled numerics, in every image build and every security advisory for the rest
of the project, for one tail probability. The implementation is the textbook continued fraction
for the incomplete beta function, and it is tested against published t-tables rather than against
itself — which is not a formality: the first version returned **negative probabilities**, because
merging the even and odd steps of the recurrence shifts it by half a term and converges to a
plausible wrong answer.

## Consequences

**Positive.** The README's claim is now true. A change is accepted when its paired interval
excludes zero and rejected when it does not, and both are printed in the terminal with the
verdict spelled out. The report on disk carries the per-question scores, so any two runs can be
compared later without re-running either.

**Negative.** The intervals are wide, and they will embarrass the dataset. Fifteen questions
across five documents cannot detect a small improvement, and the honest consequence is that the
golden set needs to grow before it can settle fine-grained arguments. Better to know that than
to keep reading four-point moves as progress.

**Discovered while building it.** A paired difference where every question improved by exactly
the same amount has a standard error of zero — and the obvious guard against dividing by it
returned a t statistic of zero, reporting the most certain possible result as indistinguishable
from noise. A test caught it, and nothing else would have: the number looked reasonable.

**Not done here.** Variance reduction by resampling each question several times, which the paper
also recommends. It costs a multiple of the run time and pays off when the per-question outcome
is noisy; retrieval at temperature zero is deterministic, so there is nothing to average out.
That changes for the judged metrics of the next batches, and it is called out again there.
