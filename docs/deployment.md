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
| **Azure OpenAI** | One chat deployment and one embedding deployment, both Global Standard. Local authentication **disabled**. |
| **Azure AI Search** | Basic tier, local authentication **disabled**. |

The first five store nothing and serve nothing: that half of the environment deploys for
about a cent an hour, which is why it went first and why the deployment path could be
exercised repeatedly before anything expensive depended on it.

The last two are where the money starts, and where this platform stops being theoretical:
it has had adapters for both since Phase 2 and has never once talked to either.

**A number you will count and question.** `what-if` reports nine changes and the portal
shows five resources. Both are right. `what-if` counts *changes* — five resources, the
resource group, and three role assignments; the portal's resource list shows what is
*inside* resource groups, and role assignments are not there. They are extension
resources, attached to whatever they grant access to, under Access control (IAM).
`az role assignment list --resource-group rg-paimon-dev` is where they show up.

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
| **Azure AI Search, Basic** | **~0.10 EUR/hour, whether or not anything queries it** |
| Azure OpenAI | Per token. Nothing while idle |

**So an afternoon is well under a euro, and a month is about 70.** That asymmetry is the
whole reason this environment is built to be destroyed: the search service bills for
existing, not for working, and it is the one resource here that will quietly consume a
trial budget while nobody is using it.

Azure OpenAI is the opposite and is worth knowing: a deployment that nobody calls costs
nothing at all, because Global Standard is billed per token. The entire fifteen-question
benchmark, answers and judgements included, is a few cents.

Still to come, for scale:

| Resource | Per month, if left running |
|---|---|
| PostgreSQL Flexible Server, General Purpose D2ds_v5 | ~150–190 EUR |

Hence `destroy.sh`, and hence `status.sh` looking at the whole subscription rather than at
the resource group you happen to be thinking about.

## Verifying the adapters against the real services

This is the step the whole phase exists for, and it does **not** need a container, a
database in Azure, or a private network. It needs a laptop.

This platform has had Azure OpenAI and Azure AI Search adapters since Phase 2, tested
against an in-process stand-in that answers with the shapes the real services are
documented to answer with. That stand-in cannot tell you whether those shapes are still
what Azure sends, whether the authentication works, or whether the index definition is
accepted. Only Azure can, and until now nothing had asked it.

So the first thing that touches these services is the benchmark, run **hybrid**: PostgreSQL
and Redis local in Docker, models and retrieval in Azure. It costs the search service's
hourly rate plus a few cents of tokens, and it exercises every line of both adapters.

```bash
docker compose -f docker/compose.yaml up -d      # postgres and redis, local
cd backend
uv sync --extra azure                            # the Entra credential is an extra

set -a; source ../infrastructure/.env.dev; set +a

export PAIMON_EMBEDDING__PROVIDER=azure
export PAIMON_CHAT__PROVIDER=azure
export PAIMON_RETRIEVAL__STORE=azure_search
export PAIMON_AZURE_OPENAI__ENDPOINT="$AZURE_OPENAI_ENDPOINT"
export PAIMON_AZURE_OPENAI__EMBEDDING_DEPLOYMENT="$AZURE_EMBEDDING_DEPLOYMENT_NAME"
export PAIMON_AZURE_OPENAI__CHAT_DEPLOYMENT="$AZURE_CHAT_DEPLOYMENT_NAME"
export PAIMON_AZURE_SEARCH__ENDPOINT="$AZURE_SEARCH_ENDPOINT"

# The index schema is the application's. Creating it is a deliberate command, and
# the workload's identity deliberately cannot do it.
uv run python -c "
import asyncio
from paimon.config import get_settings
from paimon.interfaces.api.dependencies import build_resources

async def main():
    async with build_resources(get_settings()) as resources:
        await resources.vector_store.ensure_index()

asyncio.run(main())
"

uv run python -m paimon.interfaces.cli.evaluate --answers \
    --corpus ../evaluation/corpus/sample \
    --dataset ../evaluation/datasets/retrieval-v1.jsonl \
    --label "azure openai + ai search" \
    --report ../evaluation/reports/azure.json
```

Nowhere in that is there an API key, and there is nowhere for one to go: both services were
created with key authentication switched off
([ADR-0037](adr/0037-keyless-is-enforced-at-the-resource.md)). `az login` on the laptop and
a managed identity in Azure take the same code path.

**If the first call returns 403, wait before debugging.** Role assignments take minutes to
propagate — sometimes longer — and a deployment that finished thirty seconds ago has almost
certainly not finished granting. Re-provisioning does not help and costs another cycle.

### Why this runs before the infrastructure around it

The riskiest thing in this phase is not the networking. It is two adapters that have never
been executed against the services they adapt, and everything else in the phase is built on
top of them. Finding out they work — or exactly how they do not — costs about a euro here,
and costs a rebuild of the environment if it is discovered after the database, the private
endpoint and the container app are wired around them.

Highest risk, lowest cost, first.

## What this deployment will not tell you

Stated here rather than left to be assumed:

- **Nothing that only appears after days of uptime.** Certificate renewal, log retention
  behaviour, a slow leak, a scheduled maintenance event. An environment that lives for an
  afternoon cannot observe any of it.
- **Nothing about scale.** One replica, one region, no load. The numbers this produces are
  a check that the adapters work against real services, not a performance benchmark.
- **Nothing about cost at volume.** Per-token pricing over fifteen benchmark questions
  does not extrapolate to a working day of real use.
