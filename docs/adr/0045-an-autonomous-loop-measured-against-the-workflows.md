# ADR-0045: An autonomous loop, measured against the workflows it does not replace

- **Status:** Accepted
- **Date:** 2026-09-24
- **Deciders:** Sergio Medina
- **Phase:** 9 — Autonomy, and whether it pays for itself

## Context and problem statement

[ADR-0016](0016-deterministic-workflows-before-autonomous-agents.md) chose deterministic
workflows over an autonomous loop, and wrote the condition for revisiting:

> **Revisit when** a workflow's graph starts accumulating conditional edges to cover cases
> its author did not foresee. That is the signal that the problem has become genuinely
> open-ended, and it is the point at which an autonomous loop starts paying for its cost —
> not before.

That condition is about a class of question the three workflows cannot express, not about
a graph that grew awkward. A multi-hop question — *"what does the step that decides whether
a rotation is safe actually compare?"* — needs a document that the question's own words do
not name. `incident-triage` retrieves twice with two fixed framings; it cannot follow a
pointer it finds in the first result, because which document to fetch second is decided by
what the first one said. Covering that with a conditional edge would mean one edge per
shape of indirection, which is the accumulation ADR-0016 named.

So the condition is met, and the question this ADR answers is **not** "should the platform
have an autonomous agent" but "what does one cost, and what does it buy". ADR-0016 is not
rewritten. It was right for what it decided, and its reasoning is the reason this measurement
was worth making.

## Decision drivers

- The loop must be **measurable against the alternatives**, on the same tasks, with the same
  model, or the comparison is a preference.
- It must not be able to claim an answer it cannot support. The workflows already verify
  citations; the loop must do the same, deterministically, not by asking the model.
- It must terminate for a reason that can be named, not merely stop.
- Document text must not reach the model anywhere a system instruction could be confused
  with it.
- A model that cannot call tools is a supported deployment. It gets three agents, not a
  fourth that fails on its first question.

## Considered options

1. **Do nothing.** Keep the three workflows and accept that multi-hop questions get the
   shape they fit worst.
2. **A fourth workflow with more conditional edges.** One branch per shape of indirection.
3. **An autonomous `act → tools → act` loop**, with a deterministic verification node and
   explicit stop reasons.

## Decision

**Option 3, built and measured, and kept — but not because it won.**

The `investigator` agent runs `act → tools → act` until the model stops asking for tools,
then `finalize` and a deterministic `verify`. It has six stop reasons —
`answered | step_limit | token_budget | repeated_call | retrieval_failed | no_material` —
so a run that ends says why. Tool errors return to the model as `tool` messages rather than
raising, because a model that has just been told its arguments were wrong can fix them.
Citation numbering is global across the run, so `[3]` means the same passage on turn one and
turn six. The tenant is fixed when the graph is built, not passed per call.

**What the measurement found, and it is not what the loop was built to show.**

Locally (`qwen2.5:7b-instruct`, 450 runs, agents-v1), the investigator scored **below** both
simpler systems and cost 1.9× the tokens and 3.2× the latency. Against `incident-triage` the
paired difference was −28.0% [−48.9%, −7.1%], p=0.012. On the held-out set it took **exactly
one tool call in 100 of 100 runs**, on both passage layouts — proved to be the model's choice
and not a cut loop: no limit fired, every run reached its second `act`, and a logging proxy in
front of Ollama showed that request carrying both tool definitions.

On Azure (`gpt-4.1-mini`, agents-v1, k=3) it hops — more than one call in 22 of 90 runs — and
**multi-hop accuracy does not move**: 28.6% against 28.6% for the single-pass path and 33.3%
for `incident-triage`, every pair indistinguishable from noise, at 2.4× tokens and 3× latency.

With retrieval held fixed and only the generator changed (held-out set, local bge-m3 and
pgvector), hop rate goes from 0/50 to 13/30 and pass@1 from 32.0% to 63.3% for the
investigator, and 30.0% to 76.7% for `investigator-v1` (+46.7% [+10.1%, +83.2%], p=0.018).

**So: the loop is a property the platform now has, and on the evidence available it does not
pay for itself against a well-shaped workflow on the same model.** Model capability moves the
number; autonomy does not. The agent is kept because the capability is real, the cost is
measured and small in absolute terms, and the alternative — deleting it and reporting that
autonomy was tried — would discard the apparatus that produced the finding. It is not
promoted as the default path, and `docs/evaluation.md` reports its cost beside its score.

## Consequences

### Positive

- The platform can answer a question whose second document is named only by the first.
- Every claim above is reproducible from committed reports and a committed dataset whose
  task content is hash-pinned.
- The deterministic `verify` node earns its place independently of the loop: on out-of-corpus
  tasks it turned **16 model drafts into refusals, 53% of the two investigators' correct
  refusals**.

### Negative

- It costs ~2.4× tokens and ~3× latency for no measured accuracy gain on one model.
- Its advantage is not demonstrated. The multi-hop columns are 7 tasks (agents-v1) and 6
  (held-out); "no improvement detected" is a failure to detect one, not proof of absence.
- A fourth agent is a fourth thing to keep working, and it is the only one that needs a
  tool-calling model.

### Neutral

- Registered only when the configured model can call tools; otherwise the API says why
  rather than reporting an unknown agent.

## Known limits

**Citation verification checks existence, not relevance.** `verify` withdraws an answer whose
markers do not resolve against retrieved passages. It cannot tell that a resolvable citation
fails to answer the question. Two fabrications passed it — `investigator-v1`, task a024,
trials 2 and 3 — by citing real passages from the on-call handbook to answer a question the
handbook does not cover. This is structural, not an implementation defect: a check that reads
only the citations cannot judge relevance without becoming the judge it replaced.

**The trajectory was not recorded.** Reports hold the final answer, citations and per-run
counts. Until this phase they did not hold what each tool call asked for or returned, so
"the second search reached a document the first did not" is not answerable for these runs.
Fixed going forward (`Trajectory.step_details`); see `docs/open-findings.md` §3 and §4.

## Revisit when

**The tools change, not the loop.** Across the four Azure investigator runs, 84 runs made more
than one tool call; **86% issued entirely distinct calls**, and only **47% of those reached two
documents**. The model varies its query and still does not arrive at the second document. So
the lever is not de-duplicating searches and not more turns — it is retrieval and reference
following (`read_document` on a pointer found in the first result).

Revisit this decision when that experiment has been run, which requires the trajectory
logging above. If following references raises the share of multi-hop attempts reaching two
documents and the multi-hop column moves with it, the loop starts paying for itself. If it
does not, the honest conclusion is that this corpus's multi-hop questions are answerable by
retrieval alone and the loop should be reconsidered rather than tuned.

## Alternatives in detail

**Option 1 (do nothing)** was the status quo and remains defensible on the numbers: on one
model the workflows match or beat the loop. It was rejected because it answers the question
by not asking it, and ADR-0016's condition had been met.

**Option 2 (more conditional edges)** is what ADR-0016 predicted would happen and warned
against. Each new shape of indirection is a new branch, and the graph stops being readable
long before it stops being incomplete.
