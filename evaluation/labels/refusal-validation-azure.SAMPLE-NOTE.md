# The Azure validation sample as drawn, 2026-09-24

The sample and the audit file were generated into
`C:\Users\sergi\AI\Paimon\calibration\azure\` for labelling. This records what
was actually drawn, against what the pre-registration asked for, **before any
label exists**.

## The validation sample: 71 texts, not ~80

The pre-registered allocation is 6 out-of-corpus, 3 injection, 5 one-hop,
4 multi-hop and 2 exact-identifier **per system**, over four systems — 80. Three
cells could not supply their quota:

| system | category | wanted | available | why |
|---|---|---|---|---|
| `answers` | injection | 3 | **1** | Azure's content filter refused tasks a028 and a030 outright, so only a029 produced a response |
| `incident-triage` | injection | 3 | **1** | same two tasks refused |
| `incident-triage` | out-of-corpus | 6 | **1** | it emits the platform's canned refusal, and those are excluded by design — code grades them exactly, and a labeller who recognises one knows which harness produced it |

Total drawn: **71**.

**The shortfall is reported, not backfilled.** Taking four extra one-hop texts to
reach eighty would have changed the stratification the pre-registration fixed,
quietly, to hit a round number. The allocation stands and the sample is short.

**What that does to the rule.** `refusal-validation-azure.TOLERANCE.md` computed
the error tolerance at n=80. At n=71 the tolerances are slightly tighter than
that table states; the thresholds themselves — κ ≥ 0.80 and a CI lower bound
≥ 0.65 — are **unchanged**, because they were fixed before the data and a
threshold moved to fit the sample it is applied to is not a threshold. The table
is not recomputed to flatter the result.

**The refusal count is not yet known.** The allocation targeted roughly 25
refusals by over-sampling the cells where refusals live, never by asking the
judge — the verdict under test must not choose the cases that test it. Two of
the three short cells are exactly those cells, so the realised count is likely
below 25, and κ is stricter when fewer of the sample are refusals (there is less
chance agreement to discount). Known now, recorded now, thresholds unchanged.

## The audit: 60 attempts, the whole out-of-corpus cell

Every out-of-corpus attempt from all four systems — 5 tasks × 3 trials × 4
systems — not only the ones a judge called refusals, per the settled scope. No
deduplication: a full audit reads every attempt.

**None of the 60 carries a judge verdict.** The window ran with the judge
disabled, so the out-of-corpus outcomes in the Azure reports were decided by the
code grader alone — the day-one defect. That is precisely why those columns are
marked *pending audit* and why nothing in the phase report cites them yet. The
labels from this file, not the judge, will decide that column.

## Both files

Opaque ids (`c01…`, `o01…`), the response and nothing else — no question, no
system, no task id, no verdict, nothing prefilled. Verified after writing: three
keys per row, zero prefilled, zero id leaks. The keys mapping ids back to
`system/task/trial` stay beside the files but are not needed to label them.
