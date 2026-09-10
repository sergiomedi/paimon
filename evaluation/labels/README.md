# Human labels

A person's verdicts on a sample of answers, used to measure how far the evaluation judge agrees
with somebody who read the same material ([ADR-0032](../../docs/adr/0032-a-judge-is-uncalibrated-until-a-person-checks-it.md)).

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
