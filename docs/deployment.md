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
| **Container registry** | Basic tier. `publish.sh` builds the image into it, in Azure rather than on your machine. |
| **Container Apps environment** | Workload profiles, Consumption profile. Hosts the API, the migration job, and the collector in the next batch. |
| **Azure OpenAI** | One chat deployment and one embedding deployment. Local authentication **disabled**. |
| **Azure AI Search** | Free tier by default, local authentication **disabled**. |
| **Virtual network** | Two subnets: one the Container Apps environment is injected into, one holding private endpoints. |
| **PostgreSQL Flexible Server** | General Purpose. **No password and no public address** — Entra only, reachable through a private endpoint. |
| **Container app** | The API and the MCP endpoint, with a Redis sidecar (ADR-0039). External ingress, scales to zero, `secrets: []`. |
| **Container apps jobs** | Two, both manual, both the same image as the API: one bootstraps the database role and extensions (ADR-0042), one runs `alembic upgrade head` (ADR-0040). |
| **A second managed identity** | A database administrator, attached to the bootstrap job and to nothing that serves traffic. |
| **Application Insights** | Workspace-based, into the same workspace as the logs. **Local authentication disabled** — ingestion needs a token, not a key. |
| **OpenTelemetry collector** | The upstream contrib image, internal ingress only, the one thing allowed to write telemetry (ADR-0041). |

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

# Azure refuses to create a resource type whose provider is not registered, with
# an error that does not obviously say so. Registering takes a few minutes and
# only has to happen once per subscription.
for ns in Microsoft.ManagedIdentity Microsoft.OperationalInsights Microsoft.KeyVault \
          Microsoft.ContainerRegistry Microsoft.App Microsoft.CognitiveServices Microsoft.Search \
          Microsoft.Network Microsoft.DBforPostgreSQL; do
    az provider register --namespace "$ns"
done
```

### The app registration, which no template can create

The API validates Microsoft Entra tokens and a deployed environment **refuses the
development identity provider outright** — there is no bypass to fall back on. So an
application to issue tokens for has to exist, and an Entra app registration is not an ARM
resource, so this is the one prerequisite that is genuinely manual:

```bash
APP_ID="$(az ad app create --display-name paimon-api --query appId -o tsv)"

# The application ID URI is what clients name when asking for a token, and what the
# API checks the 'aud' claim against. They have to be the same string.
az ad app update --id "$APP_ID" --identifier-uris "api://$APP_ID"

export AZURE_PAIMON_API_AUDIENCE="api://$APP_ID"
```

**The URI has to contain the app id, the tenant id, or a verified domain.** A memorable one
like `api://paimon` is refused outright under Microsoft's default tenant policy:

```
All newly added URIs must contain a tenant verified domain, tenant ID, or app ID,
as per the default tenant policy of your organization.
```

`api://<app id>` always satisfies it, which is why the commands above use it and why this
value has to be **exported** — it cannot be a default in the parameter file, since it is
different in every tenant. Export it before `deploy.sh`: the audience is baked into the
container's configuration rather than read at runtime, so a deployment made without it
produces an API that refuses every token, and fixing it is another deployment.
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
export AZURE_PAIMON_ENV=dev              # names and tags everything
export AZURE_PAIMON_LOCATION=swedencentral
```

**Region is not a latency decision, and it is not only a quota decision either.** Ask
rather than choose:

```bash
./scripts/azure/regions.sh
```

It asks each candidate region **two** questions and reports which will take the deployment.
Nothing is created; about ten seconds a region.

Two questions, because one is not enough. `validate` checks the template, the providers,
ordinary SKUs and whether the region is open to the subscription — and returns success for a
region with no model quota at all. The first version of this probe reported ten usable
regions for a subscription that could not deploy a chat model in any of them. So it reads the
quota list as well, and a region is only green when both agree.

That script exists because picking a region from documentation has failed three times here,
each for a different reason and each discovered only by trying:

| | |
|---|---|
| Sweden Central | The models were in its catalogue and there was **no quota** for the deployment type the template asked for. |
| Sweden Central | No **Basic search capacity** either. |
| West Europe | Had both — and then stopped **accepting new customers**, which fails every resource in the template including the managed identities. |

None of those is a property of the region. Each is a property of *this subscription, in that
region, on that day*, which is why no table in any document can answer it and why the probe
asks Azure instead. `RequestDisallowedByAzure` with a link to `aka.ms/locationineligible` is
the third one's signature, and it is not quota: the region is simply closed to the
subscription, and the fix is another region rather than a smaller request.

### What can actually be deployed here

Three things decide whether a model deployment succeeds, and a subscription can have plenty
of one combination and **zero** of the neighbouring one:

**the model** × **the deployment type** × **the region**

The catalogue and the quota are different questions. `az cognitiveservices model list` says
what exists in a region; this says what you may create:

```bash
az cognitiveservices usage list --location westeurope -o json > /tmp/quota.json
python3 - <<'EOF'
import json
rows = json.load(open("/tmp/quota.json"))
usable = [r for r in rows if (r.get("limit") or 0) > 0]
print(f"{len(rows)} quotas, {len(usable)} with a limit above zero\n")
for r in sorted(usable, key=lambda r: r["name"]["value"]):
    print(f'{r["name"]["value"]:<62} {r["currentValue"]:>7} / {r["limit"]}')
EOF
```

The rows are named `OpenAI.<deployment type>.<model>`. A subscription that shows
`OpenAI.Standard.text-embedding-3-large → 350` and has a row for
`OpenAI.GlobalStandard.text-embedding-3-large` at **limit 0** has 350 thousand tokens per
minute of that model and cannot deploy a single one of it under Global Standard. That is not
an edge case; it is what this project hit on its first attempt and again on its third, and it
is why `embeddingSku` and `chatSku` are parameters rather than constants — and why the
defaults are now `Standard` for the embedding model and `GlobalStandard` for the chat model,
which is the only combination this subscription actually holds.

**The quota row is not named after the model.** The catalogue calls it `gpt-4.1-mini`; the
quota row is `OpenAI.GlobalStandard.gpt4.1-mini` — no hyphen after `gpt`. Searching the quota
list for the model's own name finds nothing, which reads exactly like having no quota, and it
cost two days here. `regions.sh` normalises both spellings so nobody has to remember this.

One more row worth finding before planning anything: `OpenAI.S0.AccountCount`. On a trial
subscription it is often **1 / 1** — one Azure OpenAI account, total. A soft-deleted one
still counts against it, which makes the purge in `destroy.sh` the difference between
redeploying and not.

## The commands

```bash
./scripts/azure/regions.sh    # which regions will take this deployment at all
./scripts/azure/preview.sh    # can this be deployed, and what would it change
./scripts/azure/deploy.sh     # the same two checks, then asks, then deploys
./scripts/azure/publish.sh    # build the image into this environment's registry
./scripts/azure/bootstrap.sh  # create the workload's database role and extensions
./scripts/azure/migrate.sh    # bring the database schema up to date
./scripts/azure/status.sh     # what exists right now, anywhere in the subscription
./scripts/azure/destroy.sh    # delete, purge, and prove nothing is left
```

### A new environment takes five of them, in this order

```bash
./scripts/azure/deploy.sh     # everything except the application
./scripts/azure/publish.sh    # build the image
./scripts/azure/deploy.sh     # again, now with the application
./scripts/azure/bootstrap.sh  # the workload's database role, once per environment
./scripts/azure/migrate.sh    # create the schema
```

There is no manual SQL step. There was one until ADR-0042, and it could not
actually be carried out: it asked the Entra administrator to connect from inside
a network their laptop is not in.

**Why twice.** The registry the application pulls from is created *by* this template, so on
the very first pass there is nowhere an image could already have been pushed to. Rather than
deploy a Container App pointed at an image that does not exist — which succeeds, then fails
every revision it starts, silently, minutes later — `deploy.sh` asks the registry what is in
it and simply leaves the application out of the first pass. It tells you so, and tells you
what to run next. This is the same shape as `azd provision` followed by `azd deploy`, for
the same reason.

After that, a code change is two commands: `publish.sh` then `deploy.sh`. The image is
tagged with the commit it was built from — with a `-dirty` suffix when the tree had
uncommitted changes — so "which build is deployed" has an answer, and it is in the
deployment outputs as `AZURE_API_IMAGE_DEPLOYED`.

**The build happens in Azure.** `publish.sh` uses `az acr build`, which uploads the build
context and runs the Dockerfile on ACR's own agents. No Docker daemon is needed locally,
which matters on WSL without Docker Desktop and in CI, and the image never crosses the
network as layers.

`preview.sh` asks Azure **two different questions**, and the distinction is worth holding on
to. *Validation* asks whether this deployment is possible at all — quota, SKU availability,
resource providers, property-level correctness. *what-if* asks what would change if it were.
This project shipped with only the second, and it cheerfully predicted sixteen resources in
a region that could not have created two of them. Both create nothing and take seconds.

`deploy.sh` is idempotent: names derive from the subscription and the environment name, so
running it twice updates rather than duplicates. It writes the deployment outputs to
`infrastructure/.env.<environment>` — git-ignored, and how later steps find the registry,
the identity and the environment without anyone copying values around.

**Read the `what-if` output rather than trusting it.** It has two documented blind spots:
it cannot resolve `reference()` expressions and reports them as changes, and Azure's own
post-submission defaults appear as deletions of properties nobody set. Both are noise;
neither makes the preview useless, and a genuine `Delete` line is worth stopping for.

### Two failures worth recognising

Both were hit on the first real deployment of this template. `deploy.sh` explains each one now
rather than printing the JSON, but they are worth knowing.

**`AadAuthOperationCannotBePerformedWhenServerIsNotAccessible`.** The database was created and
was not yet ready to accept a Microsoft Entra administrator. This is a race
[Azure has had open since 2023](https://github.com/Azure/azure-postgresql/issues/127): a
flexible server reports `Succeeded` before it can take principal operations, and `dependsOn` is
satisfied by that `Succeeded`. Nothing is broken and the template is not wrong.

**Run the same command again.** The deployment is idempotent, the server already exists, and
the second pass finds it ready. The template also orders the administrators behind the private
endpoint and its DNS zone group, which is both the honest dependency — an administrator cannot
be added to a server nothing can reach — and several minutes of settling time. That makes the
race unlikely rather than impossible.

**`AadAuthPrincipalCreationFailed ... 42710: role already exists`.** A PostgreSQL role name
stops at **63 characters**, and Azure registers a Microsoft Entra administrator under its
sign-in name. A guest account's UPN —
`someone_gmail.com#EXT#@tenantname.onmicrosoft.com` — runs well past that, so the role is
created truncated and every later deployment asks for a name PostgreSQL does not have while
the truncated one blocks creating it. Reruns do not help; nothing about it converges.

`registerOperatorAsAdministrator` is **off** because of this, and turning it off is the fix.
Nothing is lost: since [ADR-0042](adr/0042-a-second-identity-that-exists-to-grant-one-privilege.md)
the administrator that does the work is the management identity, whose name is short by
construction, and since [ADR-0038](adr/0038-a-database-with-no-password-and-no-public-address.md)
the server has no public address, so a person's account could not reach it from a laptop
whether or not it were registered. Turn it on only for a sign-in name comfortably under 63
characters, on a deployment where somebody works from inside the network.

**`unable to pull image using Managed identity`.** A container is attached to an identity that
has no `AcrPull` on the registry. Only the workload identity holds it, so the bootstrap job —
which runs as the administration identity in order to reach the database — is attached to
**both** and names the workload's in `registries[].identity`. Pulling an image and reaching a
database are separate acts with separate credentials, and giving the administration identity a
registry role to save a line would widen an identity that exists to hold exactly one privilege.

**`Because vector isn't a trusted extension, only members of "azure_pg_admin" are allowed to
use CREATE EXTENSION`.** Raised by a *migration*, which runs as the workload. The bootstrap
creates the extension, so there is nothing left to do — but `CREATE EXTENSION IF NOT EXISTS` is
still refused, because Azure enforces the trusted-extension rule **before** PostgreSQL gets as
far as noticing the extension is installed. `IF NOT EXISTS` protects against the extension
existing; it does not protect against not being allowed to ask. A migration therefore queries
`pg_catalog.pg_extension` and skips, which is what the initial migration now does.

**`FlagMustBeSetForRestore`.** A soft-deleted Cognitive Services account still holds the name.
Purge it rather than restoring it, and note that on a trial subscription it is also holding the
only account you are allowed to have:

```bash
az cognitiveservices account list-deleted -o table
az resource delete --ids "$(az cognitiveservices account list-deleted \
  --query "[?starts_with(name, 'oai-paimon')].id" -o tsv)"
```

`az cognitiveservices account purge` needs the name, the resource group **and** the region to
all be right, and says nothing at all when any of them is not — which is how one survived a
purge that appeared to work. `status.sh` lists them, and the list is the only proof.

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
| Azure AI Search, **free** tier | Free. 50 MB, three indexes, no standard semantic ranker |
| Azure AI Search, Basic | ~0.10 EUR/hour, **whether or not anything queries it** |
| Azure OpenAI | Per token. Nothing while idle |
| Virtual network, private endpoint | Free, and about 0.01 EUR/hour respectively |
| Container app, **idle** | Free. `minReplicas` is 0, so nothing runs and nothing bills |
| Container app, **one replica running** | ~0.02 EUR/hour for 1 vCPU and 2 GiB, and the first 180,000 vCPU-seconds a month are free |
| Migration job | Per second while it runs, which is seconds |
| OpenTelemetry collector | ~0.01 EUR/hour. It does **not** scale to zero, on purpose |
| Application Insights | Per GB into the workspace above; the first 5 GB a month are free |
| **PostgreSQL, General Purpose D2ds_v5** | **~0.25 EUR/hour, and it is the reason destroy.sh exists** |

**On the defaults, an afternoon is well under a euro and a month is around 180.** Almost all
of it is the database: everything else here is free while idle or billed per token.

The application is in the second category on purpose. `minReplicas: 0` means an environment
nobody is using costs nothing for the application, and the price is a cold start on the
first request after roughly five minutes of quiet — an image pull, a process start and two
connection pools opening. That is worth knowing *before* the first latency measurement
rather than after it: the first request is not a measurement of anything.

Two shapes of cost, and the difference is the one that matters here. Azure OpenAI bills per
token: a deployment nobody calls costs nothing at all, and the entire fifteen-question
benchmark with judgements is a few cents. A Basic search service bills for **existing**,
which is the shape that quietly consumes a trial budget while nobody is using it.

`searchSku=basic` is needed for the full benchmark corpus and for the standard semantic
ranker. The sample corpus fits in the free tier, so the default is free — and the reason to
know the difference is that switching to basic turns an environment that costs nothing while
forgotten into one that costs 70 EUR a month while forgotten.

Hence `destroy.sh`, and hence `status.sh` looking at the whole subscription rather than at
the resource group you happen to be thinking about.

## Letting the workload into the database

```bash
./scripts/azure/bootstrap.sh
```

The database has no password, so the workload authenticates with a Microsoft Entra token —
and a token only works once a **role exists for that identity inside PostgreSQL**. That is
SQL rather than ARM, so no template can do it, and since the database has no public address
there is nowhere outside the network to run it from either.

So it runs as a job, as a **second managed identity that exists only for this**
(ADR-0042). The identity is a database administrator; the workload's is a plain user, which
keeps the property worth keeping: the thing a role is created for is not the thing that
creates it.

The job does three things, all idempotent:

| | |
|---|---|
| Creates the `vector` extension | An administrator's privilege, and one the workload should not hold. Doing it here makes the migration's own `CREATE EXTENSION IF NOT EXISTS` a no-op rather than a failure. |
| Creates the workload's role | On a connection to the server's **`postgres`** database, because that is the only place Azure installs the `pgaadauth` functions. By **object id**, not by display name — a service principal's display name is not unique in a tenant. |
| Grants the database **and the schema** | Both, because neither implies the other. PostgreSQL 15 revoked `CREATE` on `public` from `PUBLIC`, so without the second grant the migration authenticates perfectly and fails on its first `CREATE TABLE`. |

**Two databases, and that is not an implementation detail.** Azure installs
`pgaadauth_create_principal` and its relatives **only in the server's own `postgres`
database**. A connection to the application's database reports them as not existing, with an
`UndefinedFunctionError` whose hint suggests adding explicit type casts — which sends you
inspecting argument types for a function that is not there at all. A PostgreSQL role is
cluster-wide, so the role is created there and takes effect everywhere; an extension and a
schema grant belong to one database, so those happen on the application's own.

Run it again whenever you are unsure whether it ran. It reports what already existed and
changes nothing.

**It can fail on timing alone.** Registering an Entra administrator and assigning a role
both take minutes to become effective, so a bootstrap started the moment a deployment
finishes may be too early. Retrying is safe and is usually the whole fix.

It comes **before** `migrate.sh`: the migration authenticates as the workload and fails the
same way without it, which is why each script names the other when it fails.

## Migrating the schema

```bash
./scripts/azure/migrate.sh
```

Starts the migration job, waits for it, and prints its logs. The job runs the deployed image
with `alembic upgrade head`, inside the virtual network, because since the database lost its
public address there is no route to it from anywhere else (ADR-0040).

It is **manually triggered**, not run on every deployment and not on a schedule. A migration
is a decision; Phase 8 is where the decision gets a gate in front of it rather than being
removed.

Two things follow from the job running the same image as the API. The image carries
`alembic.ini` and `migrations/`, so the schema's history travels with the code that assumes
it. And Alembic's `env.py` builds its engine through the application's own `build_engine`
rather than from a URL — a URL is enough with a password and not enough with an Entra token,
which is fetched per connection. Handing Alembic a DSN would have worked on every laptop and
failed in every deployed environment.

## Getting a cost figure out of it

Cost is not measured. It is token counts multiplied by a table somebody typed, and the
provider's invoice is the authority — so the table is empty by default and no cost is reported
at all, because a model absent from the price list produces silence rather than a zero.

To fill it, read the pricing page on the day and pass the numbers in, per **million** tokens,
which is the unit providers publish:

```bash
export AZURE_PAIMON_MODEL_PRICES='{"gpt-4.1-mini":{"input":0.4,"output":1.6}}'
export AZURE_PAIMON_PRICE_REVISION="$(date -u +%Y-%m-%d)"
./scripts/azure/deploy.sh
```

The revision label is required as soon as there are prices, and it is recorded on every
measurement: a cost figure that cannot be traced back to the table that produced it becomes
uninterpretable the moment the table changes. The numbers above are an example of the shape,
not a quotation.

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

## A measured run, end to end

The environment exists to be measured once and destroyed, so this is the whole
sequence in one place, with the things worth writing down as they go past. Budget
a couple of hours and perhaps five euros; most of that is the database billing
while you work.

**Before the clock starts.** `az login`, the providers registered, the app
registration created, and the quota table read for the region you are using.
Everything above this section. None of it bills.

```bash
./scripts/azure/preview.sh                 # can this be deployed at all
./scripts/azure/deploy.sh                  # everything except the application
./scripts/azure/publish.sh                 # build the image, in Azure
./scripts/azure/deploy.sh                  # again, now with the application
./scripts/azure/bootstrap.sh               # the database role and extensions
./scripts/azure/migrate.sh                 # the schema
```

Between the second `deploy.sh` and `bootstrap.sh`, give role assignments a few
minutes. A bootstrap that fails on propagation is safe to retry and usually needs
nothing else.

**Then, in order, with what to record beside each:**

| | Record |
|---|---|
| `./scripts/azure/status.sh` | What exists, and what it bills per hour. |
| Ingest a document and ask a question through the API | The wall-clock time of the **first** request, which is a cold start: image pull, process start, pool open. It is the number nobody publishes and everybody meets. |
| Ask the same question again | The warm number. The gap between the two is the interesting figure, not either one alone. |
| Open Application Insights | That traces arrived at all, and what one request's span tree looks like end to end. If it is empty, read the collector's logs before anything else. |
| The hybrid benchmark, below | Retrieval and answer quality against the real services rather than against Ollama. This is the number worth quoting, because it was measured against what a deployment would actually use. |
| Cost Management in the portal | Actual spend for the session, per service. Not the estimate — the invoice. |

Then `./scripts/azure/destroy.sh`, and `./scripts/azure/status.sh` once more to
prove the table is empty. The second one matters: it is what turns "I destroyed
it" into something checked rather than assumed.

**Write the numbers down before the environment goes away.** Every figure above
becomes unavailable the moment the resource group does, and the whole argument
for provisioning, measuring and destroying is that the measurements outlive the
environment.

## What a trial subscription will not do

Four of this phase's failures were not bugs, not quota in the ordinary sense, and not
anything a template can express. They are restrictions Azure applies to subscriptions funded
by free credit, and they are collected here because each one cost a deployment to find.

| | |
|---|---|
| **Model quota is granted per model × deployment type × region**, and most combinations are zero. `GlobalStandard.text-embedding-3-large` is zero in every European region here; `Standard` has 350 thousand tokens per minute. | `scripts/azure/regions.sh` |
| **Entire regions stop accepting new customers.** West Europe refused every resource in the template, managed identities included, with `RequestDisallowedByAzure`. | `scripts/azure/regions.sh` |
| **One Azure OpenAI account, total.** `OpenAI.S0.AccountCount` is 1 of 1, and a soft-deleted account still holds it. | `scripts/azure/status.sh` |
| **ACR Tasks do not run.** `az acr build` returns `TasksOperationsNotAllowed`: Microsoft [suspended task runs funded by free credits](https://learn.microsoft.com/en-us/answers/questions/1684863/acr-tasks-requests-for-the-registry-are-not-permit) in 2024 and the official answer is a pay-as-you-go subscription. `publish.sh` falls back to a local Docker build and push. |

None of these is documented where you would look for it before starting, and none of them
appears in a price list. The pattern is worth stating plainly: **a trial subscription is not a
small paid subscription.** It is a different product with restrictions that only surface when
something is refused, and a deployment designed against the documentation will meet them one
at a time.

Everything here works on a pay-as-you-go subscription without changes. Nothing in the template
was changed to accommodate these; what changed is that the scripts now ask Azure first, and
explain the refusal when it comes.

## What this deployment will not tell you

Stated here rather than left to be assumed:

- **Nothing that only appears after days of uptime.** Certificate renewal, log retention
  behaviour, a slow leak, a scheduled maintenance event. An environment that lives for an
  afternoon cannot observe any of it.
- **Nothing about scale.** One replica, one region, no load. The numbers this produces are
  a check that the adapters work against real services, not a performance benchmark.
- **Nothing about cost at volume.** Per-token pricing over fifteen benchmark questions
  does not extrapolate to a working day of real use.
- **Nothing about requests longer than four minutes.** Container Apps closes an ingress
  connection at 240 seconds and that number belongs to the platform. An agent run that takes
  longer is cut off by the infrastructure rather than by the application, and the answer is
  streaming or a job — not a larger timeout, because there is no timeout property to raise.
- **Nothing about a shared cache.** Redis runs beside each replica rather than between them
  (ADR-0039), so a hit rate measured at one replica is not the hit rate at three.
- **Nothing about whether the telemetry arrived.** The collector's health endpoint reports that
  it is accepting data, not that it is delivering it. An exporter that cannot authenticate looks
  healthy and drops everything, and it says so only in its own logs. If Application Insights is
  empty, `az containerapp logs show --name ca-paimon-otel-<env>` is the first place to look, not
  the second.
