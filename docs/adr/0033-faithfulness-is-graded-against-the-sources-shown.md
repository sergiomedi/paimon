# ADR-0033: Faithfulness is graded against the sources shown, completeness against the golden set

- **Status:** Accepted
- **Date:** 2026-09-10
- **Phase:** 6 — Evaluation
- **Amends:** [ADR-0031](0031-a-judge-on-terms-that-account-for-what-it-is-worth.md)

## Context and problem statement

ADR-0031 built a judge and made it **reference-guided**: it grades against a fixed anchor
rather than against its own idea of a good answer, which the literature consistently finds
more reliable. That part was right. The anchor was wrong.

The anchor it used was the passage the golden set names for each question. ADR-0013 wrote
those passages to score **retrieval** — one quoted sentence per question, deliberately
minimal, chosen so that ground truth would survive a change of chunk size. They are an
excellent answer to *did the retriever find the right place*. They were never a description
of what a corpus supports.

ADR-0032 then required that a person check the judge before its numbers are quoted. Somebody
did, on fifteen answers, and the calibration said what calibration is for:

```
faithfulness  0.667 ± 0.135        kappa +0.45 (TOO LOW), raw 73.3%
relevance     1.000 ± 0.000        kappa +1.00 (acceptable), raw 100.0%
```

Reading the disagreements against the corpus settled it. Every claim the judge and the
labeller had marked unsupported was **in the corpus, verbatim** — a question about kernel
upgrades whose golden passage is *"Nodes are drained before any kernel upgrade, without
exception"* was answered with the drain procedure, the reboot step and the reason for it,
all of them sentences from the same document. Across fifteen answers there were no
fabrications at all. The 0.667 measured the brevity of the golden set.

That is not a bad judge. It is a well-behaved judge answering a question nobody wanted asked:
*does this answer stay inside one sentence somebody quoted for a different purpose*. And it
penalised the behaviour the platform exists to produce — grounded elaboration from retrieved
material — while being unable to distinguish it from invention.

## Decision drivers

- A metric that cannot separate *elaborated correctly* from *made it up* is not measuring
  hallucination, which is the only reason to have this metric.
- The rubric a person is asked to apply must be one they can apply twice the same way. The
  labeller's own verdicts drifted on the small additions and held on the large ones, which
  is what happens when the task is "spot every phrase absent from one sentence".
- The judged section is compared across runs and eventually published. Publishing a number
  under a name the field uses for something else is a claim, not a shorthand.

## What the term means elsewhere

Faithfulness is a named metric with a settled meaning, and it is not this one.

- **Ragas** defines faithfulness as *"how factually consistent a response is with the
  retrieved context"*, scored as claims supported by the retrieved context over total claims.
- **Azure AI Foundry** separates two evaluators and contrasts them explicitly: *groundedness*
  measures how well a response aligns with the **given context** without fabricating content
  — "the precision aspect" — while *response completeness* measures how completely it covers
  the expected information compared to **ground truth** — "the recall aspect".

Both take precision against retrieved context and recall against a reference answer to be
different measurements. This platform had been computing precision against the *reference*,
which is neither of them, under the name of one of them.

## Considered options

1. **Widen the golden passages** so that everything the corpus supports appears in them. This
   is writing the corpus twice, it goes stale the moment a document changes, and it destroys
   what the golden set is for: `recall@k` becomes meaningless when the expected passage is
   the whole document.
2. **Keep one number and accept it as strict.** Honest only if the number is labelled as
   something other than faithfulness, and it still cannot separate the two failure modes.
3. **Two metrics, each against the evidence its question is about.**

## Decision

Option 3.

**Faithfulness is graded against the sources the model was shown.** Not the retrieved set —
the sources that *fitted in the prompt*, in marker order, which is a smaller list whenever
the context budget cut something. `Answer` now carries them, because they were previously
built inside the use case and discarded, and nothing outside it could say what the model had
actually seen. The numbering is the answer's own marker numbering, so `[2]` in an answer
names `[2]` in the judge's prompt and the judge can follow one to the other.

**Completeness is graded against the golden passages**, and is a new metric rather than a
renaming: it asks whether the answer *carries* what those passages say. That is the question
the golden set was written to answer, and it catches the failure the old metric never could —
an answer that invents nothing because it says nothing.

**Relevance is unchanged**, and is still shown no passages at all.

The three verdicts are taken per answer, where the answer is in hand. The previous
implementation judged in a second pass over the reports, which is how the golden passages came
to be the only thing available to grade against; a report does not carry a prompt.

**Every calibration is per rubric.** One agreement figure for the judge as a whole would have
averaged +1.00 and +0.45 into something reassuring and sent the next person to fix the wrong
rubric. The labelling file follows: each verdict is optional and a blank one is skipped for
its own metric, so labelling one rubric across every case — which is both how people work and
what the guidance on rater agreement recommends — produces a calibrated figure for that rubric
and an honest absence for the others.

**The labelling template carries both sets of evidence**, marked as what they are. Asking a
person to grade three questions off one set of passages is what made the old faithfulness
number impossible to reproduce by hand, and a judged number a person cannot reproduce is a
judged number nobody can calibrate.

## On changing a metric after seeing a bad number

This deserves the suspicion it attracts, so: the evidence is independent of the direction the
number moves. The claims are in the corpus or they are not, and that was checked by opening
the files; the field's definition of the word differs from the implementation, and that was
checked against published documentation. Neither fact would read differently if the old number
had been 0.95.

And the change is not a loosening. Completeness is a number the old design never reported, it
is not one a wordy answer wins by default, and the strict question the old metric was
accidentally asking — *did this answer stay inside the golden passage* — was never a question
worth a threshold.

The numbers produced before this ADR are not comparable with the ones after it, and none of
them are quoted anywhere.

## Consequences

**Positive.** The two failure modes are now separable, which is the whole point: an answer
that fabricates scores badly on faithfulness, an answer that omits scores badly on
completeness, and an answer that does both is visibly different from either. The
faithfulness rubric now describes a task a person can do repeatably — read the sources, read
the answer, look for a claim the sources do not carry — which is what makes calibration mean
something.

**Negative.** The report and the label file grow: the sources are now recorded on every case.
That is deliberate rather than tolerated — a judged number whose evidence was discarded cannot
be re-checked by a person later, and re-checking it is the whole of ADR-0032 — but it is a
real cost on a large dataset, and a run over a corpus with a long context budget will produce
a large report.

**Negative.** Labels made under the old rubric do not transfer, including the fifteen that
found this. They were verdicts on a question that is no longer asked.

**Negative.** Faithfulness against the retrieved sources cannot catch an answer that
faithfully repeats a passage that should never have been retrieved. That is correct — it is a
retrieval failure, and `recall@k` and `precision@k` own it — but it means a high faithfulness
score is not on its own a statement that the answer is true.

**Not done here.** Whether a *particular* marker is the right source for the sentence carrying
it is still unjudged. Citation resolution is verified by opening the offsets (ADR-0030), and
the faithfulness rubric deliberately treats a claim supported anywhere in the sources as
supported, so an answer that cites the right corpus in the wrong order is not penalised. A
per-claim attribution judgement is a third rubric and a larger labelling burden, and it should
wait until this one has a kappa worth trusting.
