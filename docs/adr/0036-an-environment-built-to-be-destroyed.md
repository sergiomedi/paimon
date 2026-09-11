# ADR-0036: The environment is built to be destroyed, and the teardown is a feature

- **Status:** Accepted
- **Date:** 2026-09-11
- **Phase:** 7 — Cloud

## Context and problem statement

This platform is deployed to Azure to prove two things that cannot be proved locally: that
the Azure adapters work against the real services rather than against the in-process
stand-in their tests use, and that the deployment architecture described in this repository
actually stands up.

Neither of those requires the deployment to keep existing afterwards.

The budget is a fixed amount of trial credit. The resources that make this system real —
Azure AI Search, PostgreSQL Flexible Server — bill **for every hour they exist**, whether
or not a request reaches them. A search service on the Basic tier and a General Purpose
database left running for a month cost more than the entire budget for this phase. Left
running for an afternoon, they cost less than a coffee.

So the question is not "how cheap can the deployment be". It is "what is the shape of a
deployment that is meant to end".

## Decision drivers

- Fixed budget, no top-up. Running out means the phase cannot be finished.
- The measurement is what has value: latencies, costs, whether the adapters behave. Once
  recorded, the resources that produced them have no further use.
- Whatever is deployed has to be deployable again — by the author next week, and by anyone
  reading this repository.
- A teardown that is a manual checklist is a teardown that is performed incorrectly at
  midnight.

## Considered options

1. **A permanent low-cost environment.** Burstable database, free search tier, scale to
   zero. Perhaps 40–60 EUR a month. It is a live demo, which has real value for a portfolio
   — but the free search tier is 50 MB and three indexes, and Microsoft is explicit that
   the Burstable database tier is not for production and supports no high availability. So
   the thing left running is a demo described as production, which is the one claim this
   repository has been careful not to make anywhere else.
2. **A permanent honest environment.** Around 250–300 EUR a month. It would consume the
   entire budget in under three weeks and then stop.
3. **An ephemeral environment, deployed to be measured and then removed.** A few euros for
   each cycle of a few hours. The deliverable is the template, the measurements, and the
   record of what it cost.

## Decision

Option 3, and the teardown is treated as a first-class part of the deliverable rather than
as something a reader is trusted to do.

**Names are deterministic.** Every resource name derives from the subscription and the
environment name, so redeploying produces the same environment rather than a second one.
That is what makes `deploy` safe to re-run — and it is precisely why the teardown has to do
more than delete a resource group.

**The teardown purges, not just deletes.** A key vault is soft-deleted: its name is held
for the retention window, and the next deployment either fails on the name or silently
recovers the old vault with whatever was in it. Azure OpenAI accounts behave the same way
and additionally hold their custom domain. `scripts/azure/destroy.sh` deletes the group,
waits for it, purges both, and then lists whatever is left anywhere in the subscription
tagged as belonging to this platform. An empty table is the point of the script.

**Purge protection on the key vault is deliberately off.** It cannot be turned off once
enabled, and it would make the vault's name unusable for the retention window after every
teardown. On a permanent environment it is a safety feature; here it would be a trap, and
the template says so where the property is set.

**Every resource is tagged `lifecycle: ephemeral`.** It is a standing answer to the
question somebody eventually asks about a resource group they have found: yes, it is safe
to delete.

**There is a `status` script, and it looks at the whole subscription.** Not at this
environment's resource group — at anything tagged as this platform's, anywhere. The
expensive mistake is not forgetting to destroy the environment currently being worked in.
It is the one created last week under a different name.

**The measurements are committed; the environment is not.** What `docs/deployment.md`
carries after the final batch is the numbers, the cost, and the commands that reproduce
both. That is the artefact. The resources were the instrument.

## Consequences

**Positive.** The deployment path is exercised repeatedly rather than once, which is the
only way to know it works: an environment stood up a single time and left running hides
every idempotency bug it has. The cost of this phase is bounded by attention rather than by
time. And "I deployed it, measured it, wrote down what it cost, and removed it" is a more
honest claim than a URL that happens to still resolve.

**Negative.** There is no live demo at the end of this. For a portfolio that is a genuine
loss, and it is the reason option 1 was considered seriously rather than dismissed. The
mitigation is that the repository carries the architecture, the template, the measurements
and the guide — and a reader with an Azure subscription can have the whole thing running
with one command.

**Negative.** Anything that only manifests after days of uptime — certificate renewal,
log retention behaviour, a slow leak — is not observed. That is stated in the deployment
guide rather than left for a reader to assume otherwise.

**Negative.** Deployment history accumulates in the subscription across cycles. Harmless,
and noted so that a reader is not surprised by twenty deployments of the same name pattern.

**Revisit when.** If this platform ever gets a permanent environment, this ADR is what has
to change first: purge protection goes on, the deterministic naming stays, `destroy.sh`
stops being a routine operation, and a deployment stack with `actionOnUnmanage: detachAll`
replaces the trust currently placed in a confirmation prompt.
