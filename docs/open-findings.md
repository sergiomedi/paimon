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
