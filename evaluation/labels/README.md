# Human labels

A person's verdicts on a sample of answers, used to measure how far the evaluation judge agrees
with somebody who read the same material ([ADR-0032](../../docs/adr/0032-a-judge-is-uncalibrated-until-a-person-checks-it.md)).

One JSON object per line:

```json
{"case_id": "q001", "question": "...", "answer": "...", "expected_passages": ["..."],
 "faithfulness": "yes", "relevance": "partial", "note": ""}
```

- **faithfulness** — does the answer only say what the passages support? `yes` / `partial` / `no`.
  An answer that declines to answer is `yes`: refusing is not an unsupported claim.
- **relevance** — does it address the question asked? Correctness is not the question here;
  something wrong but on-topic is `yes`.
- Leave both blank to skip a case. Do **not** write `undecided` — a blank is skipped, an
  `undecided` would be counted.

Generate a template with `--write-labels`, and pass the finished file back with `--labels`. The
template does not show the judge's verdict, on purpose.
