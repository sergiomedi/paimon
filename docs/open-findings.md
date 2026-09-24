# Open findings

Things this project has measured, understands, and has **not** fixed. Each one
names what was observed, what it costs, and what the minimum fix would be, so
that a later phase can pick it up without re-deriving it — and so that nobody
reads silence as absence.

Nothing here is fixed in the phase that found it. A finding recorded and
deferred is a decision; a finding fixed in passing, at the end of a phase, on
the strength of a plausible cause, is how the measurement that found it stops
being trustworthy.

---

## 1. `grounded: true` is reported for answers that cite nothing

**Observed.** Phase 9, local agent run, 2026-09-23. The `grounded` metric counts
an attempt as grounded when it carries at least one citation — but a refusal
carries none, and the correct refusal of an out-of-corpus question is scored the
same way as an answer that failed to cite. In the agent reports this shows as
`grounded: true` on attempts whose text is "the sources do not cover this".

**What it costs.** `grounded` cannot be read as a quality score on any dataset
containing refusal tasks, which is every dataset this project ships.
`docs/evaluation.md` already says the metric is not a quality score, so nothing
published is wrong — but the field is easy to misread and is reported beside
numbers that *are* quality scores.

**Minimum fix.** Report `grounded` over answerable attempts only, or split it
into `grounded` and `declined` so the denominator is visible. Not a rename: the
problem is the denominator, not the word.

**Status.** Open. Own step, not Phase 9.

---

## 2. Azure OpenAI's content filter turns a retrievable document into an error

**Observed.** Phase 9, Azure agent window, 2026-09-24. Six of ninety `answers`
attempts came back `400 — the prompt triggering Azure OpenAI's content
management policy`, all on tasks `a028` and `a030`, whose corpus documents carry
a deliberately injected instruction. `incident-triage` failed on the same two
tasks, the same three trials each, with the same 400 from the same endpoint in
the log.

**What it costs, and why it is a product finding rather than a test artefact.**
The filter fires on the *retrieved document*, not on the user's question. So on
Azure, **any** question whose retrieval happens to pull in that document returns
an error to the caller — not a refusal, not a degraded answer, an error. The
README promises a system that answers from its corpus or declines; it does not
promise one that fails when the corpus contains something the provider dislikes.
Nothing restricts this to the injection tasks: the document is in the corpus,
and what retrieval returns depends on the question.

**A lead, unverified, written down rather than acted on.** Azure applies
different handling to an attack in the user's own prompt than to one embedded in
a document — the latter is what Prompt Shields calls an indirect attack. Paimon
deliberately places document text in the **user** turn (it is what makes an
injected instruction inert against the system prompt, and it is tested), which
may be why the filter reads it as user intent. **This has not been confirmed**,
and it should be, before any change is made on the strength of it.

### 2a. An agent run reports no reason for its own failure

**A defect in its own right, not a detail of the one above.** The single-pass
path surfaces the provider's message — `azure openai chat returned 400 (…content
management policy…)`. The agent path reports **`the run failed`** and nothing
else: no status code, no provider message, no indication whether the cause was
the platform, the network, the model or Paimon.

**What it costs.** An operator whose run fails learns nothing from the failure.
They cannot tell a refusal they should stop retrying from an outage they should
retry, and the same string covers both. It is worse than the single-pass path at
exactly the moment the caller needs more, not less.

**What it cost this measurement**, concretely: the tasks had to be excluded from
a paired comparison, and the report alone could not justify excluding them. The
proof had to be recovered from the run log by correlating HTTP 400s against
benchmark progress lines — `scripts/azure/filtered.py --log`. Where that proof
is absent the failure is **counted against the system** rather than excluded,
because excluding it would be excluding it on a guess.

**Minimum fix.** The same one as above — a distinct *the provider refused this
content* outcome carried adapter → agent → report → API — plus, more generally,
an agent failure that records its cause at all.

**It is not only an injection-category problem.** The filter is counted by
system **and** category, because the three systems retrieve differently and an
injected document can surface for any question.

**Minimum fix.** A distinct outcome — *the provider refused this content* —
carried from the adapter through the agent path to the report and the API,
instead of a generic failure. That is what lets a caller tell "the platform
would not process this" from "the system broke", and what lets a benchmark
exclude such tasks from a paired comparison rather than scoring them as agent
failures.

**What was done instead, in Phase 9.** Nothing to the product. The measurement
excludes affected tasks from paired comparisons and reports both versions, with
and without. The 400 is deliberately **not** retried — it is a deterministic
refusal, and a test pins that.

**Status.** Open. Own step, not Phase 9.

---

## 3. A report records what an agent answered, not what it did

**Observed.** Phase 9, throughout. An agent report stores, per attempt, the final
answer, its citations, and counts — `stop_reason`, `tool_calls`,
`repeated_calls`, the node sequence, tokens, latency. It does not store the tool
trajectory: **not the arguments of each call, and not the ids of the documents
each call returned**. `agent_runs` holds `steps`, `answer` and `citations`, and
there is no checkpoint table, so the messages are gone when the process exits.

**What it costs.** It blocks the next experiment this phase identified. Across
the four Azure investigator runs, 84 runs made more than one tool call; 86% of
them issued entirely distinct calls, and only 47% of *those* reached two
documents. So the model varies its query and still does not arrive at the second
document — but whether a given search **returned a new document or the same one
again** cannot be answered, because what each call returned was never recorded.
"Distinct call" is recoverable; "new document" is not.

It also cost accuracy in this phase's own prose: reports were described as
holding "transcripts", which is true of the answer and citations and false of
the trajectory. `docs/measurements/agents-2026-09-23.md` now says exactly what is
stored.

**Minimum fix.** Record, in each tool step's details, the call's arguments and
the ids of the documents it returned. That is what turns "the second search was
different" into "the second search reached a document the first did not", which
is the measurement that would say whether the lever is retrieval, reference
following (`read_document`), or the loop.

**Status.** Open. Own step, not Phase 9. It is the precondition for the next
experiment, not a side issue.

---

## 4. Integration tests destroyed a finished measurement's evidence — **fixed**

**Observed.** Phase 9, 2026-09-24. The withdrawn drafts of every agent run in
this phase are gone. `verify` records the text it withdrew in that step's
details, those details lived only in `agent_runs`, and
`tests/integration/test_postgres_agents.py` opens with
`TRUNCATE TABLE agent_runs, agent_memories`. Running `scripts/check.sh` after
the Azure window — several times — emptied the table.

**What it cost.** The measurable claim is now *"the deterministic citation check
turned 16 model drafts into refusals on out-of-corpus tasks (53% of the
investigators' correct refusals)"*. What cannot be said is how many of those 16
drafts were **fabrications** rather than disclaimers, which is the number that
would price the check. The reports themselves are files and are intact, so no
figure in this phase is wrong; only this one cannot be computed.

**It was the second time.** In Phase 2 the same suite truncated `chunks` and
`documents` mid-benchmark and produced a plausible, entirely false result. The
answer then was `verify_corpus()`, a pre-flight guard on those two tables. It did
not generalise: the next destructive test truncated a different table.

**Fixed, both halves.**

1. *The tests cannot reach a real database.* The integration fixture calls
   `require_disposable()` before anything connects, and refuses any database
   whose name does not end in `_test` — upstream of every `TRUNCATE`, so it does
   not matter which table a future test decides to empty. `check.sh` and CI
   create and use `paimon_test`. A unit test (which runs with no database
   present) pins the refusal, that it names the database and says why, and that
   the message tells a contributor the command to fix it.
2. *The evidence is not kept where a test may empty it.* `Trajectory` now
   carries `step_details`, every step's recorded details, written into the
   report file and preserved across a regrade. The report is the record of the
   experiment; the database is application state. Two tests pin it: a withdrawn
   draft appears in the written report, and it survives being read back.

**Status.** Fixed in Phase 9. Recorded because the loss is permanent and the
reasoning belongs with the numbers it limits.

---

## 5. No judge in this project is calibrated against a person

**Observed.** 2026-09-24. [ADR-0032](adr/0032-a-judge-is-uncalibrated-until-a-person-checks-it.md)
sets the rule that a judge is uncalibrated until a person has checked it. Phase 6 reported κ
figures against `evaluation/labels/answers-v1.jsonl` as though the bar had been cleared.

Those fifteen labels were **not labelled by a person: the project owner confirms he did not
label them; origin unrecorded, most likely an AI assistant in an earlier session.** No commit,
and no file beside them, records who produced them. The development journal states that the
owner did ("Fase 6 · Tanda 5"); that text was written by that session's assistant and is false.

**What it costs.** Every calibrated figure in the project compares one rater with another where
neither is known to be human:

- Phase 6 (faithfulness κ +0.45, completeness κ +0.81, relevance κ +1.00) — rater unknown.
- Phase 9's refusal judge (κ 0.648 for llama3.1:8b, 0.918 and 0.892 for phi4) — labelled by
  Claude (Opus), stated as such, and for the Azure sample **not blind to category**.

None of the numbers is withdrawn. What is withdrawn is the claim that any of them answers
ADR-0032's question. κ measures whether two raters agree, not whether either is right, and
where both raters are language models it does not measure what the ADR asked for at all.

**The second-order problem, which is the one worth remembering.** An assistant wrote into the
project's own journal that a person had done work that person had not done, and it stood for
two weeks. Nothing in the repository could have caught it: provenance was not recorded, so
there was nothing to contradict. That is why every label file produced since carries a
provenance note naming its rater, and why the directory README now lists them one by one.

**Minimum fix.** Fifteen to twenty labels by an actual person on `answers-v1`, and a sample of
the Phase 9 refusal texts, would turn one of these into a calibration in ADR-0032's sense. Until
then the honest statement is the one now in `docs/evaluation.md`: by that ADR's own rule, no
judge here is calibrated against a person.

**Status.** Open. The false claims are corrected; the missing calibration is not.
