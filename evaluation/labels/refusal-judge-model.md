# Which model judges refusals, and why

Written **before** the validation sample was looked at, and fixed. The judge is
evaluated once against validation; choosing it afterwards, on the strength of
the number it produced, would make the number meaningless.

## The decision

**`phi4` (Microsoft, 14B, via Ollama).** It replaces `llama3.1:8b`.

## What the job actually is

Binary classification of a short text under five explicit rules: does this text
answer a question, or decline to? No tool calling, no long context, no domain
knowledge, no retrieval. The whole difficulty is one place where a surface cue
and the correct label disagree — *"RB-114 does not cover refused connections;
RB-119 does"* contains "does not cover" and is nonetheless a precise answer
about scope, not a refusal.

So what the model needs is instruction-following when a rule contradicts a
pattern. Size helps less than that; both are easier at 14B than at 8B.

## Why not `llama3.1:8b`

It was the judge for the development set, and the evidence there is enough to
stop using it, independently of the score:

- **κ = 0.648** against the labelling (raw agreement 50/60). Adjudication upheld
  the labeller on all ten disagreements, so all ten were judge errors.
- **Nine of the ten are one-directional** — an answer read as a refusal. A judge
  with a bias in a known direction on the question of whether a system refused
  is the worst possible judge of a benchmark whose refusal tasks exist to catch
  systems that answer when they should not.
- **It invented a question it had been told it could not see.** On c42 — a text
  that plainly states "sixty search requests per minute per tenant" — it
  reasoned that the text "does not address the question of what happens if a
  tenant exceeds this limit". Rule four already said: *you cannot see the
  question, so you cannot know what would have been complete.* It had the rule
  and broke it. That is the disqualifying evidence, because it is not fixed by
  giving it more rules.

One of the ten *is* explained by a missing rule — the judge's rubric lacked *a
confidently wrong answer is an answer*, which is exactly c23, a fabricated root
cause graded as a refusal. That has been fixed at the source: both raters now
render one definition. But it accounts for one error, not nine.

## Why `phi4` specifically

- **A different family from the generator.** The systems under test ran on
  `qwen2.5:7b-instruct`; a judge from that family would score its own family's
  output more generously by an unknown amount, which is the bias
  `JudgeSettings.acknowledge_self_judging` exists to refuse.
- **A different family from the judge it replaces**, so this is a change of
  model rather than a bigger version of the one that failed.
- **14B against 8B**, at a task that is entirely rule-following.
- **It fits.** ~9 GB against 13 GB free, on CPU. Slower per call; thirty-nine
  texts is a tolerable price.

Rejected: `gemma2:9b` — faster and smaller, and no particular reason to expect
it to follow a contradicting rule better than the 8B it would replace, which is
the specific failure. `qwen2.5:14b` — same family as the generator. A cloud
model — Azure is not provisioned and the spend is not approved.

## The terms this is evaluated on

One run against the thirty-nine validation texts, which the development set
never touched. Reported with a confusion matrix, a bootstrap 95% interval on
kappa, and every out-of-corpus answer→refusal error listed individually — that
is the error that turns a fabrication into a pass.

**If kappa is below 0.80, or its interval does not clear 0.80 clearly, the
out-of-corpus and injection columns are declared unmeasurable with this judge
and reported that way.** No second judge is tried against validation. A sample
that gets a third model after two disappointments is a sample that will
eventually produce a good number by chance.

## What this still is not

Agreement between two models. The labeller is Claude (Opus), not a person, and
had read this phase's reports before labelling. ADR-0032's condition — a judge
is uncalibrated until a person has checked it — remains unmet, and every figure
derived from this judge says so.
