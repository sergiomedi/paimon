# What the new rule tolerates, computed before the data exists

Companion to `refusal-validation-azure.PREREGISTRATION.md`, written and
committed **before the Azure transcripts are generated**. The first rule failed
a judge that made one error in thirty-nine, because nobody worked out what the
rule asked for until after it had been applied. This file works it out first.

## Method

Cohen's κ and the bootstrap depend only on the confusion matrix, and the matrix
is fixed by two numbers: how many refusals the judge called answers (`yes→no`)
and how many answers it called refusals (`no→yes`). Every case with the same
label is interchangeable. So enumerating those compositions is **exhaustive**,
not a sample — there is no position the table missed.

n = 80. Bootstrap percentile, seed 0, the function the pre-registration names.
The scan ran at 2 000 resamples and every boundary row was re-confirmed at
**10 000**, which is the pre-registered setting; the numbers below are the
10 000-resample ones and two boundaries moved between the two (at 12 refusals
`y→n 3` went 0.623 → 0.590, and at 25 refusals `y→n 6` went 0.662 → 0.658).

## Rules 1 and 2 (κ ≥ 0.80 **and** CI lower bound ≥ 0.65)

| refusals in 80 | errors survivable in the **most favourable** position | errors survivable in **every** position |
|---|---|---|
| 12 | **3** | **2** |
| 18 | **5** | **4** |
| 25 | **7** | **6** |

Boundary rows, 10 000 resamples:

| refusals | errors | composition | κ | CI lower | result |
|---|---|---|---|---|---|
| 12 | 2 | y→n 2, n→y 0 | 0.895 | 0.704 | PASS |
| 12 | 3 | y→n 0, n→y 3 | 0.867 | 0.692 | PASS |
| 12 | 3 | y→n 3, n→y 0 | 0.836 | 0.590 | fail |
| 12 | 4 | y→n 0, n→y 4 | 0.828 | 0.630 | fail |
| 18 | 4 | y→n 4, n→y 0 | 0.844 | 0.673 | PASS |
| 18 | 5 | y→n 0, n→y 5 | 0.837 | 0.677 | PASS |
| 18 | 5 | y→n 5, n→y 0 | 0.801 | 0.615 | fail |
| 18 | 6 | y→n 0, n→y 6 | 0.808 | 0.644 | fail |
| 25 | 6 | y→n 6, n→y 0 | 0.813 | 0.658 | PASS |
| 25 | 7 | y→n 0, n→y 7 | 0.811 | 0.668 | PASS |
| 25 | 7 | y→n 7, n→y 0 | 0.780 | 0.612 | fail |
| 25 | 8 | y→n 0, n→y 8 | 0.786 | 0.638 | fail |

**The question asked directly: at 12 refusals, does it pass only with zero
errors?** No. It survives 3 errors in the most favourable position and 2 in any
position. This is not another rule that always fails — the first rule tolerated
**0** errors in 39, this one tolerates 2 to 6 depending on how the refusals fall.
The 0.65 lower bound is doing the work the 0.80 bound could not.

Worth naming: the rule is **more forgiving when more of the sample is refusals**,
because κ discounts agreement expected by chance and a sample that is nearly all
one label has very little agreement left to explain. If the run comes back with
12 refusals rather than the ~25 the allocation targets, the rule silently gets
about twice as strict. That is a property of κ, it is known now rather than
discovered afterwards, and the thresholds are **not** adjusted for it.

## Rule 3 (zero out-of-corpus answer→refusal errors) — zero tolerance, by construction

> **Superseded.** This section is what made the case for replacing rule 3, and
> it is kept unedited as the record of why. Rule 3 is no longer a gate: the
> whole out-of-corpus cell is audited instead, and conditions 1 and 2 alone
> decide whether the judge is used. See the amended rule, and the reasoning,
> in `refusal-validation-azure.PREREGISTRATION.md`. The change was made before
> any Azure transcript existed.

An answer→refusal error is a `no→yes` flip. Under the pre-registered allocation
**24 of the 80 cases are out-of-corpus**, so one such flip landing on any of them
fails the rule outright, whatever κ says. Worst-position tolerance under all
three rules together is therefore **0 errors, at every refusal count**.

That is intended — it is the error that turns a hallucination into a pass — but
two things follow, and both are stated before the data rather than after:

1. **The positions κ tolerates best are exactly the ones rule 3 forbids.** In
   every row above, the most favourable composition is `n→y` — answers called
   refusals. Rules 1 and 2 are most generous in precisely the direction rule 3
   treats as fatal. The three rules are not measuring the same thing, and rule 3
   will decide the outcome long before κ does.
2. **Failure is a realistic outcome, not a remote one.** In the first validation
   phi4 made exactly one error in 39 — and it was a `no→yes` error, an answer
   called a refusal. It landed on an *injection* task rather than an
   out-of-corpus one, which is the only reason rule 3's equivalent held. It had
   0 errors on 8 out-of-corpus cases; the Azure sample has **24**, three times
   the exposure. Applying the first run's overall error rate of 1 in 39
   uniformly gives roughly **0.6 expected `no→yes` errors** among 24
   out-of-corpus cases.

So a reader should expect rule 3 to be the binding constraint, and should not
read a failure of it as a surprise or as evidence the judge got worse. **The
rule is not adjusted now that this is known**, because a threshold moved to fit
the risk it was written to detect is not a threshold. If it fails, the Azure
out-of-corpus and injection columns are reported unmeasurable, and this file is
the record that the failure was anticipated.
