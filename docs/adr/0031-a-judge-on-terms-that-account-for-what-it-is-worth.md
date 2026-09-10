# ADR-0031: A judge, on terms that account for what a judge is worth

- **Status:** Accepted, amended by [ADR-0033](0033-faithfulness-is-graded-against-the-sources-shown.md)
- **Date:** 2026-09-05
- **Phase:** 6 — Evaluation

> **Amended.** Everything below about *what terms make a judge's output worth reporting* still
> holds. What it got wrong is which reference the faithfulness rubric grades against: this ADR
> anchored it to the passage the golden set names, which is a one-sentence retrieval anchor, and
> the first calibrated run showed that scoring a correct elaboration against it is
> indistinguishable from scoring an invention. ADR-0033 moves faithfulness onto the sources the
> model was actually shown and adds completeness for the question the golden set does answer.

## Context and problem statement

ADR-0030 verified everything a lookup can settle. Two questions survive it and cannot be
answered by arithmetic: whether the sentence around a resolving citation is a **fair reading**
of the passage, and whether the answer **addresses the question**. Both need a judgement.

The evidence about what such a judgement is worth is not encouraging. On the closest measured
comparison — TREC 2024 RAG, GPT-4o against human assessors on whether a passage supports a claim
— they agreed **56%** of the time, and the model erred towards **over-crediting** support. The
broader literature adds position bias measured across roughly 150,000 evaluation instances,
verbosity bias, and family bias: models score their own provider's outputs more generously.

None of that makes a judge useless. It makes an *unqualified* judge useless. The question this
ADR answers is what terms make its output worth reporting.

## Decision drivers

- A judged number and a verified number must not be read the same way.
- Every known bias should be met with a mitigation, or acknowledged in the output.
- A judge that breaks must not look like an answer that failed.
- The benchmark must stay runnable and free by default.

## Considered options

1. **Adopt a framework's judge** — RAGAS or similar, with its default prompts.
2. **A judge behind a port, on terms chosen against the measured failure modes.**
3. **No judge at all**, and report only what is verified.

## Decision

Option 2. Option 3 is defensible and leaves two real questions unanswered; option 1 imports
prompts nobody in this repository has read into the one place where the prompt *is* the
instrument.

The terms, each answering something measured:

**Three discrete labels, not a score out of ten.** A judge asked for a number produces one with
no stable meaning between runs. The middle label exists on purpose: forcing a binary choice on a
partly-supported answer pushes the judge towards the generous end, which is the direction it
already errs.

**Reasoning before the verdict, in that order in the JSON.** The label is written after the
argument rather than justified after the fact.

**Reference-guided.** The golden set already names the passage that answers each question, so
faithfulness is graded against a fixed anchor rather than the judge's own idea of a good answer —
consistently more reliable than prompt-only scoring. Relevance gets **no** references, because
handing them over would let the judge reward an answer for matching the expected text rather than
for answering the question.

**The rubric names the biases it is trying to avoid.** "Length is not quality" is in it, because
verbosity bias is measured and hoping is not a mitigation. So is "an answer that declines to
answer is *yes*" — without it, the platform's most honest behaviour scores worst.

**Abstention is a first-class outcome.** A reply that is not the JSON the rubric asked for
becomes `UNDECIDED` and the case is excluded, never a default label. A judge whose malformed
output silently became "yes" would be worse than no judge, because the failure would look like a
passing grade. Undecided cases are counted and printed: a judge abstaining on half a dataset
invalidates the other half.

**Repeated sampling is available and off.** Above one sample the majority verdict is taken and
disagreement is reported. **A tie is `UNDECIDED`, not broken** — breaking it towards the generous
label biases the aggregate in the direction judges already err, and breaking it the other way is
a different arbitrary rule. An even sample count is refused at startup, since it only buys ties.

**A judge that is the generator is refused at startup.** Family bias is measured, and a system
grading its own homework produces a number that flatters it by an unknown amount — unknown being
the problem, since a known bias could be subtracted. Overridable, because a deployment with one
model available is a real situation, and the report then prints `AND IT JUDGED ITSELF` in
capitals rather than a footnote.

**The judged numbers get their own section, their own caveat and the judge's name.** They are
never mixed with the verified ones. The 56% figure is printed in the report itself, because the
number is not readable without it.

**The judge is defined in the evaluation layer, not the domain.** The platform never judges at
runtime; only the benchmark does. A port on the domain would advertise a capability the product
does not have and would invite somebody to reach for a model's opinion inside an answer path
where a citation would do.

## Consequences

**Positive.** Two questions that were unanswerable now have numbers, and the numbers arrive with
enough context to be read correctly. The judge is a port, so the fake in the tests and the real
one are interchangeable, and the whole benchmark still runs with no judge configured and no cost.

**Negative.** A judged number is still a judged number. Everything above narrows the failure
modes; none of it makes the judge agree with a person more than it does. The next batch measures
that agreement rather than assuming it, which is the only honest way to know what these two
numbers are worth here.

**Discovered while building it.** The judge adapter was written into `infrastructure` first, and
`import-linter` refused it for importing the rubric from the evaluation layer. The fix was to
move the file, not to widen the rule — and moving it turned out to be right on the merits: it
depends on the `ChatModel` **port**, not on a provider, so it belongs beside its consumer.

**A smaller one, in the report itself.** The verified section printed "No model graded this run"
unconditionally, which stopped being true the moment a judge existed. A report that tells small
lies is not one anybody reads carefully, so that line is now printed only when it is true.
