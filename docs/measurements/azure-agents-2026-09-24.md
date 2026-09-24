# Measured run — agents-v1 and held-out on Azure

| | |
|---|---|
| when | 2026-09-24, 14:30–17:20 UTC |
| generator | **`gpt-4.1-mini`** (2025-04-14) via Azure OpenAI, GlobalStandard |
| embeddings | `text-embedding-3-large` via Azure OpenAI |
| retrieval | **Azure AI Search**, hybrid |
| database | PostgreSQL local in Docker — no container, no deployed application |
| refusal judge | **phi4**, run locally over the transcripts in a second pass |
| trials | 3 per task per system |
| datasets | `agents-v1.jsonl` (30 tasks) and `agents-v2-heldout.jsonl` (10 tasks) |
| cost | **$0.42 of a $9 ceiling**, plus ~€0.20 of infrastructure time |
| teardown | verified: nothing soft-deleted, resource group deleted |

The chat deployment was raised from capacity 10 to 150 before the measured run
(`sku-capacity 150`, GlobalStandard — a rate limit, billed per token, not a reservation).
At capacity 10 an earlier attempt lost **67 of 90 attempts to HTTP 429**; that run is
discarded and its numbers appear nowhere.

**The judge is phi4, calibrated against a reference labelled by Claude (Opus), not blind to
category; kappa is an upper bound.** κ = 0.892, 95% CI [0.748, 1.000], raw agreement 65/68 =
95.6%, against a rule fixed in writing before the data existed (κ ≥ 0.80 and CI lower ≥ 0.65).

## What was measured — agents-v1, four systems

| | `answers` | `incident-triage` | `investigator-v1` | `investigator` |
|---|---|---|---|---|
| one-hop (10) | 100% | 100% | 100% | 100% |
| **multi-hop (7)** | **28.6%** | **33.3%** | **28.6%** | **28.6%** |
| exact-identifier (5) | 100% | 100% | 100% | 100% |
| out-of-corpus (5) † | 100% | 100% | 86.7% | 100% |
| injection (3) ‡ | 100% ‡ | 100% ‡ | 66.7% | 66.7% |
| **overall (30)** | 82.1% | 83.3% | 77.8% | 80.0% |
| tokens / run | 1 031 | 1 320 | 2 580 | **2 460** |
| latency / run | 1.6 s | 1.7 s | 5.1 s | **4.8 s** |
| cost / run | $0.00048 | $0.00060 | $0.00119 | **$0.00115** |

† **Labelled, not judged.** All 60 out-of-corpus attempts were read individually. 58 declined
correctly; the 2 that did not are `investigator-v1`, task a024, trials 2 and 3. Code grading
had scored `answers` at **0%** here — it refused all 15 correctly, in prose, and with the
judge disabled the grader could not recognise a prose refusal.

‡ **Not comparable across the row.** Azure's content filter refused **6 of 9** injection
attempts outright for `answers` and `incident-triage` (tasks a028 and a030), so their 100% is
over **one task**; the investigators ran all 9 across three. Refused attempts are excluded,
not scored as failures.

### Paired, over the categories code grades

Restricted to one-hop, multi-hop and exact-identifier — **22 tasks** — which need no judge:

| pair | difference | p | |
|---|---|---|---|
| answers − incident-triage | −1.5% [−4.7%, +1.6%] | 0.329 | noise |
| answers − investigator | +0.0% [−13.7%, +13.7%] | 1.000 | noise |
| incident-triage − investigator | +1.5% [−12.5%, +15.6%] | 0.825 | noise |
| investigator-v1 − investigator | +0.0% [+0.0%, +0.0%] | 1.000 | noise |

Multi-hop alone (7 tasks): 28.6% / 33.3% / 28.6% / 28.6%, every pair noise.

**The autonomy does not pay for itself here.** It hops, and the hopping does not become
correct answers, at 2.4× tokens and 3× latency.

## The primary diagnostic: does a stronger model take the second hop?

| run | tool calls per run | >1 hop | 2+ documents |
|---|---|---|---|
| local, `qwen2.5:7b` (held-out) | `{1: 50}` | **0 / 50** | 11 / 50 |
| Azure, agents-v1 | `{1: 68, 2: 16, 3: 1, 4: 3, 6: 1, 8: 1}` | 22 / 90 | 17 / 90 |
| **Azure, held-out** | `{1: 10, 2: 14, 3: 1, 4: 1, 6: 1, 7: 1, 8: 2}` | **20 / 30** | **14 / 30** |

**Yes.** The local model searched once and stopped in 100 of 100 runs — proved to be its
choice, not a cut loop: no limit fired, every run reached its second `act`, and a logging
proxy in front of Ollama showed that request carrying both tool definitions.

## Isolating the model

Held-out set, **local retrieval throughout** (bge-m3 + pgvector, the same `heldout-full`
tenant as the local measurement). Only the generator changes.

| | `qwen2.5:7b` (k=5) | `gpt-4.1-mini` (k=3) | paired |
|---|---|---|---|
| `investigator` pass@1 | 32.0% | **63.3%** | +31.3% [−3.8%, +66.5%] p=0.074 |
| `investigator` multi-hop | 3.3% | **38.9%** | |
| `investigator-v1` pass@1 | 30.0% | **76.7%** | **+46.7% [+10.1%, +83.2%] p=0.018** |
| `investigator-v1` multi-hop | 16.7% | **61.1%** | |

Hop rate over both: 0/100 → 28/60. **Model capability moves the number; autonomy does not.**

## What the citation check was worth

On out-of-corpus tasks the deterministic `verify` node **turned 16 model drafts into refusals
— 53% of the two investigators' correct refusals** (8 of 15 each).

**Not "16 fabrications".** Whether each withdrawn draft answered the question or merely
disclaimed it cannot be known: the drafts were recorded only in `agent_runs`, and the
integration suite truncated that table (`docs/open-findings.md` §4, fixed).

**And it has a ceiling.** The two fabrications that passed cited real on-call handbook
passages to answer a question the handbook does not cover. `verify` checks that a citation
resolves, not that it answers.

## What cannot be concluded

- **Multi-hop is 7 tasks.** "No improvement detected" is a failure to detect one.
- **Cross-environment comparisons change three things at once** — model, search backend and
  embeddings. Only tool calls per run and documents reached are compared across
  environments, because those are mostly model-driven. Every cross-environment pass rate
  carries that confound.
- The injection row is not comparable across systems, for the reason in ‡.
- `k` differs between the local (5) and Azure (3) runs, and latency crosses local CPU against
  cloud inference, so it is not a model property.
