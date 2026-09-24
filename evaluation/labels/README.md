# Reference labels

Verdicts on a sample of answers, used to measure how far the evaluation judge agrees with a
rater who read the same material
([ADR-0032](../../docs/adr/0032-a-judge-is-uncalibrated-until-a-person-checks-it.md)).

## Who produced which of these, which is not the same for all of them

This directory was called "Human labels" until 2026-09-24, and by then it held files no person
had labelled. The three provenances are kept apart here because κ means something different
for each, and a directory-level title cannot say which.

| file | labelled by | how to describe it |
|---|---|---|
| `refusal-labels-2026-09-23.jsonl`, `refusal-validation-2026-09-24.jsonl`, `refusal-dev-adjudications-2026-09-24.jsonl`, `refusal-validation-azure-2026-09-24.jsonl`, `audit-out-of-corpus-2026-09-24.jsonl` | **Claude (Opus)** | *labelled and adjudicated by Claude (Opus); accepted by the project owner as the reference, without independent human review* |
| `refusal-validation-azure-2026-09-24.jsonl` and the audit specifically | Claude (Opus), **with the key files visible in the same folder** | additionally: *not blind to category; kappa is an upper bound* |
| `answers-v1.jsonl` (15 labels, Phase 6) | **not a person** | *not labelled by a person: the project owner confirms he did not label them; origin unrecorded, most likely an AI assistant in an earlier session* |

**`answers-v1.jsonl` was not labelled by a person: the project owner confirms he did not label them; origin unrecorded, most likely an AI assistant in an earlier session.**

It was committed in `9132dfc` on 2026-09-10; neither that commit, nor any other, nor any file
beside it, records who filled in the fifteen verdicts. Git authorship settles nothing — every
commit in this repository carries the project owner's identity, including the ones an
assistant wrote. Asked directly, **the project owner confirms he did not label them**. The
development journal claims he did ("Fase 6 · Tanda 5"); that text was written by that
session's assistant and is false.

So the figures calibrated against it (faithfulness κ +0.45, completeness κ +0.81, relevance
κ +1.00) are **not** evidence that a person checked this judge. By the rule in ADR-0032 —
see the erratum at the top of it — no judge in this project is calibrated against a person
today. Recording who labelled a reference set is now part of
producing one; `refusal-labels-2026-09-23.PROVENANCE.md` and
`refusal-validation-azure.LABELLER.md` are what that looks like.

One JSON object per line:

```json
{"case_id": "q001", "question": "...", "answer": "...",
 "sources": ["...", "..."], "expected_passages": ["..."],
 "faithfulness": "yes", "completeness": "partial", "relevance": "yes", "note": ""}
```

## The three verdicts, and what each is graded against

They are graded against **different fields of the same line**, and mixing them up is what made
an earlier version of this file produce a number nobody could reproduce
([ADR-0033](../../docs/adr/0033-faithfulness-is-graded-against-the-sources-shown.md)).

- **faithfulness** — against `sources`, the numbered passages the model was shown. Does the
  answer say anything those passages do not carry? A claim supported *anywhere* in them counts
  as supported, whether or not the answer pointed at that source. Detail is not invention: an
  answer that elaborates from the sources is `yes`. An answer that declines to answer is also
  `yes` — refusing is not an unsupported claim.
- **completeness** — against `expected_passages`, the passages the golden set names. Does a
  reader of the answer learn what they say? Wording need not match, and extra material is not
  your concern here. A refusal is `no`: it conveys nothing they carry.
- **relevance** — against neither. Does it answer *this* question? Correctness is not the
  question; something wrong but on-topic is `yes`.

Markers like `[1]` are formatting for all three. Length is not quality for any of them.

## How to work through it

Label **one rubric at a time**, all the way down the file, then start again with the next.
Holding three rubrics in mind at once is how a labeller drifts — small additions get waved
through and large ones get flagged, which is the same rule applied two different ways.

Every verdict is optional. A blank one is skipped for its own metric, so a file with
faithfulness filled in and nothing else is a calibrated faithfulness figure and an honest
absence for the rest. Leave a whole line blank to skip a case. Do **not** write `undecided` —
a blank is skipped, an `undecided` would be counted. A misspelt verdict is refused with its
line number rather than read as blank.

Generate a template with `--write-labels`, and pass the finished file back with `--labels`. The
template does not show the judge's verdict, on purpose.
