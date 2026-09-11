# Deploying Paimon to Azure

> **Phase 7 is in progress.** What is described here is what exists today: the platform
> resources every other one depends on. The database, the search service, the models and
> the application itself arrive in later batches, and this guide grows with them. Nothing
> is described here as working before it works.

This deployment is **ephemeral by design**. It is created to be measured and then removed,
because the budget for this phase is a fixed amount of trial credit and the resources that
make the system real bill for every hour they exist. The reasoning is
[ADR-0036](adr/0036-an-environment-built-to-be-destroyed.md); the practical consequence is
that `destroy.sh` is as much a part of this as `deploy.sh`, and is the more carefully
written of the two.

---

## What gets created

One resource group, `rg-paimon-<environment>`, holding:

| Resource | Why it is first |
|---|---|
| **User-assigned managed identity** | Everything else's answer to "who is calling". Assigned roles before any workload exists, which is the ordering that a system-assigned identity cannot give you. |
| **Log Analytics workspace** | Where container logs land, and later where traces do. |
| **Key Vault** | RBAC-authorized, for the few secrets that cannot be designed away. |
| **Container registry** | Basic tier. The image lands here in a later batch. |
| **Container Apps environment** | Workload profiles, Consumption profile. Hosts the API, the collector, and Phase 8's migration job. |

Nothing here stores data and nothing here serves traffic, which is deliberate: the whole
batch deploys for cents, so the deployment path can be exercised repeatedly without
spending the budget on proving that it works.

## Before you start

```bash
az login
az account set --subscription "<the one you mean to spend>"
az bicep install
```

The scripts print the subscription name and id before doing anything. Read that line —
"which subscription am I in" is the question behind most of the expensive mistakes
available here.

**If `az login` fails with `AADSTS530035`**, you reached for `--use-device-code`. Microsoft
Entra's security defaults — on by default in a new tenant — block the device code flow
outright, because it is what phishing attacks abuse: *"authentication requests that use
device code flow are blocked"*. The fix is the browser flow, not a retry. From WSL, install
`wslu` so the Windows browser can be opened:

```bash
sudo apt install -y wslu
az login --tenant <tenant-id>
```

Turning security defaults off would also work and is the wrong trade: it disables enforced
multi-factor authentication for the whole tenant to work around having picked the wrong
sign-in flow.

Two optional settings, both with defaults:

```bash
export PAIMON_AZURE_ENV=dev              # names and tags everything
export PAIMON_AZURE_LOCATION=swedencentral
```

Region is worth a thought before the next batch rather than after it: **Azure OpenAI model
availability varies by region more than anything else does**, and it is the binding
constraint on this choice. Latency is not.

## The four commands

```bash
./scripts/azure/preview.sh    # what would change, without changing it
./scripts/azure/deploy.sh     # shows the same preview, then asks, then deploys
./scripts/azure/status.sh     # what exists right now, anywhere in the subscription
./scripts/azure/destroy.sh    # delete, purge, and prove nothing is left
```

`deploy.sh` is idempotent: names derive from the subscription and the environment name, so
running it twice updates rather than duplicates. It writes the deployment outputs to
`infrastructure/.env.<environment>` — git-ignored, and how later steps find the registry,
the identity and the environment without anyone copying values around.

**Read the `what-if` output rather than trusting it.** It has two documented blind spots:
it cannot resolve `reference()` expressions and reports them as changes, and Azure's own
post-submission defaults appear as deletions of properties nobody set. Both are noise;
neither makes the preview useless, and a genuine `Delete` line is worth stopping for.

## Destroying it

```bash
./scripts/azure/destroy.sh
```

Deleting the resource group is **not enough**, and this is the part that surprises people
on the second deployment rather than the first:

- A **key vault is soft-deleted**. Its name is held for the retention window, and the next
  deployment either fails on the name or silently recovers the old vault with its contents.
- **Azure OpenAI accounts** behave the same way and additionally hold their custom domain.

So the script deletes the group, waits for it to finish, purges both kinds of soft-deleted
resource, removes the local outputs file, and then lists everything tagged
`application=paimon` **anywhere in the subscription**. An empty table is the point.

Purge protection on the vault is deliberately off. It cannot be turned off once enabled and
would hold the name for the retention window after every teardown — a safety feature on a
permanent environment, a trap on this one.

## What it costs

Approximate, Sweden Central, and worth re-checking against the pricing page rather than
trusting a table in a repository.

| Resource | While it exists |
|---|---|
| Managed identity | Free |
| Container Apps environment | Free while nothing runs on it |
| Key Vault | Effectively free at this volume |
| Log Analytics | Per GB ingested; the first 5 GB a month are free |
| Container registry (Basic) | ~0.15 EUR/day |

**So this batch is roughly 1 cent an hour.** That is the point of it going first: the
deployment mechanism gets exercised before anything expensive depends on it.

The expensive resources arrive in the next batch. For scale, and why this phase is run the
way it is:

| Resource | Per month, if left running |
|---|---|
| Azure AI Search, Basic | ~70 EUR |
| PostgreSQL Flexible Server, General Purpose D2ds_v5 | ~150–190 EUR |
| Azure OpenAI | Per token — a few euros for an entire benchmark run |

An afternoon of both is around one euro. A month of both is more than the budget for this
phase. Hence `destroy.sh`, and hence `status.sh` looking at the whole subscription rather
than at the resource group you happen to be thinking about.

## What this deployment will not tell you

Stated here rather than left to be assumed:

- **Nothing that only appears after days of uptime.** Certificate renewal, log retention
  behaviour, a slow leak, a scheduled maintenance event. An environment that lives for an
  afternoon cannot observe any of it.
- **Nothing about scale.** One replica, one region, no load. The numbers this produces are
  a check that the adapters work against real services, not a performance benchmark.
- **Nothing about cost at volume.** Per-token pricing over fifteen benchmark questions
  does not extrapolate to a working day of real use.
