# Who labelled this sample, and what that is worth

The sixty labels in `refusal-labels-2026-09-23.jsonl` were produced by
**Claude (Opus, in Cowork)**, at the maintainer's request, following the rules in
the template's own instructions, without sight of the key or of the judge's
verdicts.

**This is not a calibration against a person.** It is agreement between two
models of different families — the judge under test, and Claude as the second
rater. ADR-0032's condition is that a judge is uncalibrated until a person has
checked it; what is recorded here instead is that the project owner **accepted
these labels as the reference** without reviewing the rows. That is a decision
about what to measure against. It is not the check ADR-0032 asks for, and the
wording below never lets it be read as one.

So everywhere a number from it appears — the report, the ADR,
`docs/evaluation.md` — it is described in exactly these words:

> **labelled and adjudicated by Claude (Opus); accepted by the project owner as the reference, without independent human review**

and never as "human labels" or "reviewed by a person". The owner has read this
file and accepted these labels as the reference; that is a decision about what
to measure against, not a second pair of eyes on the rows.

## A second limitation, and it is the labeller's own disclosure

The labeller had read this phase's reports before labelling. It therefore knew
some of the out-of-corpus questions — the security-escalation one among them —
and knew the `a001` case. It was **not blind to category on every row**, which
is exactly the leak the opaque ids and the withheld questions were built to
prevent. The mechanism worked against an uninformed rater and could not work
against one that had already read the material.

That inflates agreement by an unknown amount, in the direction of the judge
looking better than it is.

## What was labelled

Sixty distinct texts, drawn from 450 attempts: 403 after the platform's own
canned refusals were removed, 124 after collapsing repeated texts, 60 sampled
across three systems and five categories. The rows carry the response only — no
question, no system, no category, no task id.

Result: **18 `yes` (declines), 42 `no` (answers)**.

## Doubtful cases, flagged by the labeller

Six on the boundary. In every one the text says the sources do not cover what was
asked and then adds related information.

| row | label | why it is doubtful |
|---|---|---|
| c13 | yes | Says where the value lives (another document) without giving it |
| c47 | yes | As c13 |
| c25 | yes | "No explicit escalation path", then infers related things |
| c44 | yes | As c25 |
| c30 | yes | "The notes give no instruction about citations" — a claim about the content, or a refusal? |
| c36 | yes | As c30 |

These six were settled by the same labeller, along with the ten development-set
disagreements. None of them turned out to be where the judge failed.

---

# The validation sample, and the adjudications

Both added 2026-09-24, and **both produced by the same labeller** — Claude
(Opus, in Cowork) — as the development labels above. Stored here rather than
left in a working folder, because a figure whose provenance lives somewhere else
is a figure that gets quoted without it.

## Validation labels — `refusal-validation-2026-09-24.jsonl`

Thirty-nine texts the development set never touched, drawn from the sixty-four
left over after it took sixty of a hundred and twenty-four. Same filters, same
opaque ids, zero overlap. Labelled **7 `yes` (declines), 32 `no` (answers)**.

Its purpose is that the sixty are now a development set: the rubric was compared
against them and corrected, so measuring the correction on them would be
measuring a judge against the exam it was tuned on.

## Adjudications — `refusal-dev-adjudications-2026-09-24.jsonl`

The ten development-set disagreements, settled: **nine upheld `no`, one upheld
`yes`** — the labeller's original call in every case. So all ten were judge
errors, and κ = 0.648 stands as measured.

**This is self-adjudication and adds no independent evidence.** The rater who
labelled the rows also settled the disputes about them, so it confirms rather
than checks. It is recorded because the confirmation is still worth having — it
rules out the possibility that the labels were casual and would not survive a
second look by their own author — and because pretending it is anything more
would be the exact failure this file exists to prevent.

A person settling these ten would be worth more than everything above.

---

# What was lost, and what the figure rests on

The development-set judge run used `llama3.1:8b` and produced **κ = 0.648**, raw
agreement 50/60. That figure is reported as measured on 2026-09-23 and is **not
recomputed**.

**The file holding all sixty verdicts no longer exists.** It was written to a
session-scoped scratchpad, which was wiped while the next run was in flight. So
was the intermediate copy of the three benchmark reports — those survived only
because they had been committed to `evaluation/reports/`, which is the argument
that was made for committing them and turned out to matter for a reason nobody
predicted.

What survives, and is committed:

- `refusal-dev-adjudications-2026-09-24.jsonl` — **all ten disagreements**, each
  with the judge's verdict, the judge's reasoning in its own words, the
  labeller's verdict, the adjudication and the full response text. That is the
  entire evidential basis for every claim made about how `llama3.1:8b` failed,
  including c42 — the text it read as not answering a question it had been told
  it could not see — which is the reason the judge was changed.
- The fifty agreements are gone. They agreed; nothing was argued from them
  beyond the count.

So κ = 0.648 is quoted here as a figure recorded at the time rather than one
that can be recomputed from what is in the repository. Re-running `llama3.1:8b`
to reproduce it would be measuring a model that has already been rejected, on a
set that has since become its development set, and would not make the number any
more checkable than it is.

Runs since then write each result to a durable journal as it is produced
(`evaluation/reports/`, gitignored), so a wipe costs minutes rather than a run.
