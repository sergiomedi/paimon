# Infrastructure

The Azure environment, as code. **How to deploy it and what it costs is in
[`docs/deployment.md`](../docs/deployment.md)** — this file is about the templates.

## What is here

| Path | What it is |
|---|---|
| `main.bicep` | Subscription-scoped entry point. Creates the resource group and calls the modules. |
| `main.bicepparam` | Parameters, read from the environment rather than written down. |
| `modules/platform.bicep` | Identity, logs, secrets, registry, virtual network, Container Apps environment. |
| `modules/data.bicep` | PostgreSQL with no password and no public address, and the private endpoint that is the only route to it. |
| `modules/ai.bicep` | Azure OpenAI and Azure AI Search, both with local authentication disabled. |
| `modules/api.bicep` | The container app, its Redis sidecar, and the two jobs: the database bootstrap and the migration. |
| `modules/observability.bicep` | Application Insights with local authentication off, and the OpenTelemetry collector that is the only thing allowed to write to it. |
| `../scripts/azure/regions.sh` | Validates this template in every candidate region and says which will take it. Creates nothing. |
| `collector.yaml` | The collector's configuration. A real file so an editor lints it and `check.sh` parses it; Bicep reads it with `loadTextContent()`. |
| `bicepconfig.json` | Linter rules, raised to errors. |
| `.env.<environment>` | Deployment outputs, written by `deploy.sh`. Git-ignored. |

## Why it looks like this

Written directly rather than assembled from Azure Verified Modules, deployed at
subscription scope, with no state file and no workflow tool. The reasoning — including
what that costs and when to revisit it — is in
[ADR-0035](../docs/adr/0035-bicep-and-templates-small-enough-to-read.md).

The environment is **meant to be destroyed**, which drives more of this than it looks:
deterministic names, soft-delete purging in the teardown, purge protection deliberately
off, and everything tagged `lifecycle: ephemeral`.
See [ADR-0036](../docs/adr/0036-an-environment-built-to-be-destroyed.md).

## Changing it

```bash
./scripts/check.sh infrastructure
```

Compiles the template and the parameter file. The linter runs inside the compiler and
`bicepconfig.json` raises its rules to errors, so this failing means a rule was broken —
not that a warning was printed. CI runs the same two commands.

Install the compiler with `az bicep install`, or the standalone CLI. Without it the check
skips with a message rather than passing silently.

Three conventions worth keeping:

- **Role definition GUIDs get a name beside them.** A bare GUID in a role assignment is
  unreviewable, and the authorization model of this environment is the deliverable.
- **Role assignment names are `guid(scope, principal, role)`.** Deterministic, so a
  redeployment updates the assignment instead of failing on a duplicate.
- **Nothing secret goes in an output.** ARM stores deployment outputs in the deployment
  history in plaintext, readable by anyone with read access to the subscription. The
  linter enforces this; the reason it is worth enforcing is that the mistake is invisible.

## What exists after each batch

Phase 7 is built in batches, and this template grows with them.

| Batch | Adds | Bills while it exists |
|---|---|---|
| 1 | Managed identity, Log Analytics, Key Vault, container registry, Container Apps environment | The registry, ~0.15 EUR/day. Nothing else. |
| 2 | Azure OpenAI with two deployments, Azure AI Search | Nothing, on the defaults: the free search tier, and models that bill per token. `searchSku=basic` makes it ~0.10 EUR/hour. |
| 3 | A virtual network, and PostgreSQL with no password and no public address | Yes — ~0.25 EUR/hour, and almost the whole bill |
| 4 | The container app, a Redis sidecar and the migration job | Nothing while idle: it scales to zero. ~0.02 EUR/hour per running replica |
| 5 | Application Insights, and the OpenTelemetry collector in front of it | ~0.01 EUR/hour for the collector, which does not scale to zero. Ingestion is per GB into the existing workspace |

Batch 2 deliberately comes before the database. The riskiest thing in this phase is two
adapters that have never been executed against the services they adapt, and the benchmark
can exercise both with PostgreSQL running locally — which makes the check cost about a euro
instead of a rebuild of everything wired around them.
