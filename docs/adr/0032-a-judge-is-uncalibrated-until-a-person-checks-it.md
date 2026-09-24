# ADR-0032: A judge is uncalibrated until a person has checked it

- **Status:** Accepted
- **Date:** 2026-09-05
- **Phase:** 6 — Evaluation

> ## Erratum — 2026-09-24
>
> **The decision below stands unchanged. What was false is that Phase 6 met it.**
>
> This ADR sets the rule that a judge is uncalibrated until a person has checked it, and
> Phase 6 then reported κ figures — faithfulness +0.45, completeness +0.81, relevance +1.00 —
> as though that bar had been cleared. It had not. The fifteen labels in
> `evaluation/labels/answers-v1.jsonl` were **not labelled by a person**: the project owner
> confirms he did not label them, their origin is unrecorded, and most likely an AI assistant
> in an earlier session produced them. The development journal's claim that he did
> ("Fase 6 · Tanda 5") was written by that session's assistant and is false.
>
> Two statements below are therefore wrong as applied to Phase 6. *"With one labeller there is
> no way to know whether a disagreement means the judge is wrong or the rubric is ambiguous"*
> presumes a human labeller who did not exist; and the framing throughout, that the judged
> numbers now carry an answer to *"and how do you know?"*, overstates what those κ figures
> support.
>
> The reasoning is not rewritten and no new ADR supersedes this one, because **no decision
> changes**: the rule was right, and it is now known to be unmet. By its own standard, **no
> judge in this project is calibrated against a person today**. See
> [`docs/open-findings.md`](../open-findings.md) and
> [`evaluation/labels/README.md`](../../evaluation/labels/README.md).

## Context and problem statement

ADR-0031 built a judge on terms chosen against every measured failure mode: discrete labels,
reasoning before the verdict, reference-guided grading, abstention on unreadable output, a
refusal to let a model grade its own family.

All of that narrows the ways it can go wrong. **None of it makes it agree with a person more
often.** And the published figures — 56% agreement on the TREC comparison — describe *other*
judges, on *other* corpora, with *other* rubrics. They establish that a judge of this kind can be
badly wrong. They say nothing about what ours is worth on this corpus with this rubric.

Reporting a faithfulness number without knowing that is the same mistake as reporting a mean
without an interval, which Phase 6 opened by fixing.

## Decision drivers

- A number nobody has checked should not read like one somebody has.
- Raw agreement flatters any rater on a skewed dataset.
- Labelling is a person's time, and there is not much of it.
- A half-finished labelling file should still be worth something.

## Considered options

1. **Trust the published figures** and report the judged numbers plainly.
2. **Calibrate against human labels**, and mark the numbers uncalibrated until that exists.
3. **Do not report judged numbers at all** without a large labelled set.

## Decision

Option 2.

**Cohen's kappa, not raw agreement.** Raw agreement is what people ask for and it is not enough:
on a dataset where nine answers in ten are faithful, a judge that says "yes" to everything agrees
90% of the time and has measured nothing. Kappa discounts the agreement two raters would reach by
chance given how often each uses each label, and a test pins exactly that case — the all-"yes"
judge scores **zero**, not ninety percent. Raw agreement is still reported, with its interval,
because a reader will want it; it is never reported alone.

**The threshold is 0.6**, which is the convention practitioners alert on. Below it the report says
the judge and the person are not reliably measuring the same thing here, and tells the reader to
treat the two numbers as an indication rather than a result. A convention chosen in advance beats
each project inventing a threshold after seeing its own numbers.

**Uncalibrated is a state the report prints.** With no labels, the judged section says
`UNCALIBRATED` and that the numbers are a figure rather than a measurement. The alternative —
printing them plainly and mentioning calibration in a document — is how a caveat gets separated
from the number it qualifies.

**The labelling template hides the judge's verdict.** Showing it would anchor the labeller, and an
anchored independent measurement is an expensive way to confirm what the model already said.
Everything else the labeller needs is on the line: the question, the answer and the passages the
golden set names, so labelling is reading one line rather than cross-referencing three files.

**A blank row is skipped; an explicit "undecided" is refused.** A half-finished file still
measures what it covers, which is what makes the task interruptible. But a person who cannot
decide should leave the case out rather than record a shrug — a shrug would be counted.

**Abstentions by the judge are skipped, not counted as disagreements.** "The judge broke" and
"the judge was wrong" are different facts and only one is about its accuracy.

## Consequences

**Positive.** The two judged numbers now come with an answer to the question a reader should ask
first: *and how do you know?* Where the answer is "nobody has checked", the report says so in
capitals rather than leaving it to be inferred.

**Negative, and it is the honest headline.** Fifteen to twenty labels is far below the 200–500
per rubric that practitioners recommend, so the kappa itself is measured imprecisely — and unlike
the retrieval metrics, no interval is put on it here, because doing that properly needs more
labels than exist. The number is a smoke test: it catches a judge that is badly miscalibrated and
cannot certify one that is well calibrated. That limit is stated in the report and in the
evaluation README rather than buried.

**Also negative.** Calibration decays. Practitioners report judges drifting within 60–90 days as
models and prompts change, so a labelled set is not a thing you build once. Nothing here enforces
re-labelling; the labels carry no date and the report does not age them. That is a gap, and it is
recorded as one.

**Not done here.** Multiple human labellers, and therefore inter-annotator agreement. With one
labeller there is no way to know whether a disagreement means the judge is wrong or the rubric is
ambiguous — and on this dataset, at this size, adding a second labeller would tell us more about
the rubric than about the judge.
