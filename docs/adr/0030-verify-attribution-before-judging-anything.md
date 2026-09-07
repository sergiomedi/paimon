# ADR-0030: Verify what can be verified; judge only what cannot

- **Status:** Accepted
- **Date:** 2026-09-05
- **Phase:** 6 — Evaluation

## Context and problem statement

The phase's goals list faithfulness, groundedness and relevance. The industry's answer to all
three is the same: ask a language model. RAGAS computes every one of its RAG metrics with an LLM
judge, and that is the shape most evaluation stacks take.

Before adopting it, it is worth knowing what such a judge is worth on the specific question
these metrics turn on — *does this cited passage support this claim?* The TREC 2024 RAG track
measured exactly that, GPT-4o against human assessors on a three-level scale: **they agreed 56%
of the time.** And the disagreement was not symmetric. The model **over-credited support**:
where a human said "no support", it tended to say "partial".

A judge that is unreliable is a problem. A judge that is unreliable *in the direction of saying
the answer was supported* is the worst possible instrument for detecting an answer that was made
up.

Meanwhile, this platform can answer a large part of the question without asking anyone. ADR-0013
anchored the golden set to quotations rather than chunk ids; the citation type carries
`document_id`, `start_char`, `end_char` and the quoted text. So "does this citation point at
what it says it points at?" is not a judgement. It is a lookup.

## Decision drivers

- The question "is this answer invented?" deserves the most reliable instrument available.
- A judge's errors are systematic, so its blind spots are the platform's blind spots.
- A number that is verified and a number that is judged should not be read the same way.
- Judged metrics cost money and time per run; verified ones cost neither.

## Considered options

1. **Judge everything**, as the common stacks do.
2. **Verify what can be verified, judge only what cannot**, and label which is which.
3. **Verify only**, and skip faithfulness and relevance entirely.

## Decision

Option 2. This batch is the verifying half; the judge arrives next, knowing what it is worth.

What is verified, by arithmetic and no model:

- **Every citation is followed into the corpus.** Open the document, go to the offsets, and see
  whether the quoted text is there. Four outcomes, and each is a different failure worth telling
  apart: *resolved*, *misquoted* (the span exists and says something else), *out of range* (the
  offsets fall outside the document), *unknown document*.
- **Every sentence is checked for a marker.** A sentence making a claim without one is precisely
  what this platform exists not to produce.
- **Markers no citation explains are counted.** The answering use case already strips these, so
  a non-empty list is a regression in that stripping rather than a model quirk.

Three details that decide whether the numbers mean anything:

**The corpus is read back from the repository, not from disk.** A citation's offsets are into the
*normalized* text the parser produced. Checking them against the raw markdown would fail for
every document the parser touched — and the failures would look exactly like a model inventing
citations, which is the one thing this must not get wrong.

**Matching is whitespace-insensitive**, for the same reason the golden set matches that way: a
chunker that normalised a line break has not invented a citation.

**An answer with no citations scores zero, not one.** Vacuous truth reads better and says an
answer citing nothing is perfectly attributed, which is exactly backwards.

The report says *"Verified, not judged"* in its own output, because the distinction is the whole
design and an ADR nobody opens cannot carry it.

## Consequences

**Positive.** The most important question a RAG evaluation asks — is this answer anchored in
something real — is answered by a check that cannot be over-credited, costs nothing per run, and
is deterministic enough to gate a build on. Every number pairs and carries an interval, like the
retrieval ones (ADR-0029).

**Negative.** Attribution is not faithfulness. A citation can resolve perfectly while the
sentence around it misreads the passage, and nothing here catches that. That is the judge's job,
and pretending otherwise would be its own kind of over-crediting.

**Not done here.** Sentence splitting is a regular expression, not a linguistic parser. The
failure mode is splitting one sentence into two, which costs a marker check a correct answer
passes anyway; a real splitter means a model or a large dependency for a rounding error.

**Refused deliberately.** Using a judge for groundedness at all, now or later. The verified check
is strictly better on this question, and adding a judged version beside it would produce two
numbers that disagree with no principled way to choose — with the judged one, on the evidence,
being the more flattering.
