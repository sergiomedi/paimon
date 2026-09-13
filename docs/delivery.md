# Delivering Paimon

> **This has run.** On 13 September 2026 a push to `main` created an environment in Azure,
> built and pushed the image, deployed it, created the database role, migrated the schema,
> obtained a real token, ingested a document, asked a question about it, and destroyed the
> environment — in **27 minutes and 46 seconds**, unattended, as
> [run 10](measurements/ci10-2026-09-13T0857.md) records.
>
> It took ten attempts. Runs 1 to 6 never reached Azure; 7 and 8 died on the identity; 9
> provisioned and then failed to authenticate to the registry. Every one of those failures
> is in this document, because the interesting part of a pipeline is not that it works — it
> is what had to be true first.
>
> Nothing is described here as working before it works — and by that standard **one half of
> this document has not run**. Everything above "How a release moves" has, twelve times
> now. The release and promotion path below it has never been executed against Azure: it is
> written, gated, and checked by `scripts/check.sh`, which is exactly what the first nine
> runs of the verification workflow also were. Its section says so where it starts.

Continuous integration has been in place since Phase 1 and is not this phase
([ADR-0006](adr/0006-continuous-integration-from-phase-1.md)). This phase is what happens
after a merge: how an image is published, how a change reaches an environment, how the
schema moves, what gates it, and how a release is undone.

The shape of it is decided in
[ADR-0044](adr/0044-delivery-without-a-standing-environment.md), against a constraint most
descriptions of continuous delivery do not have: **there is no environment to deliver
into**. This platform's environment costs about six euros a day for a database nobody is
querying, out of a fixed amount of trial credit, and Phase 7's whole argument is that it
should not exist when nobody is measuring it.

So delivery here is two separate things, and keeping them separate is the design:

| | |
|---|---|
| **Verification**, on every merge to `main` | An entire environment is created under a run-specific name, bootstrapped, migrated, called through with a real token, and **destroyed in the same job** — whether it passed, failed or was cancelled. |
| **Promotion**, when somebody decides | A gated workflow that deploys to a named environment and leaves it running. Whether a deployment exists is a spending decision, and spending decisions are not made by a merge. |

## Why the verification is the whole chain

Phase 7 found four defects by deploying, and **not one of them was visible to a linter, a
type checker, a contract test or a unit test**. A guard against configuration typos rejected
four valid variables and the container exited 1 two seconds into every start. The Entra
adapter passed a string describing a key object to PyJWT instead of a key, so every real
token had been rejected since Phase 1. The search adapter created its index with a POST
where the service requires a PUT. An app registration created by the documented commands
could not be asked for a token at all.

Three of those four were hidden by an in-process stand-in that agreed with the code it was
meant to check. A pipeline that lints a template and builds an image would have been green
for every one of them.

That is why the verification job runs the real sequence — deploy, publish, deploy,
bootstrap, migrate, authenticate, ingest, answer — against real Azure services, and why it
is worth twelve minutes and a few cents per merge.

## Signing in without a secret

The pipeline authenticates to Azure with **OpenID Connect**. There is no client secret, no
`AZURE_CREDENTIALS` blob, and nothing in the repository to rotate: GitHub mints a
short-lived token for each run, and Entra is told in advance which repository, branch and
environment it will trust.

Run once, by a person:

```bash
./scripts/azure/federate.sh <owner>/<repository>
```

It creates the app registration, its service principal, one federated credential per trusted
subject, and the two role assignments the deployment needs. Then it prints three values to
store as repository **variables** — not secrets, because an application id, a tenant id and
a subscription id are identifiers and holding all three gets nobody a token.

Two details are worth knowing before they cost an afternoon:

- **The subject is matched exactly.** `repo:owner/name:ref:refs/heads/main` is a different
  subject from `repo:owner/name:pull_request`, and only the subjects federated above can
  obtain the credential. A pull request from a fork therefore cannot deploy anything, which
  is a property of the mechanism rather than a rule somebody has to enforce.
- **The workflow needs `permissions: id-token: write`.** Without it GitHub mints no token
  at all and `azure/login` fails on an empty assertion — an error that does not mention the
  permission it is missing.

The pipeline's identity holds **Contributor** and **Role Based Access Control
Administrator** at the subscription. The second one surprises people: deploying resources
and creating *role assignments* are separate rights, and this template creates several,
because every identity in the platform is granted what it needs by the deployment rather
than by hand. Without it the deployment fails partway through, having created most of an
environment. Role Based Access Control Administrator rather than Owner, because it grants
exactly that one thing.

## The verification workflow

`.github/workflows/delivery.yml` runs on every merge to `main` that changes anything which
can change what runs — documentation and evaluation reports are excluded, which is a cost
control rather than a shortcut. It does exactly what a person did by hand throughout Phase 7:

```
deploy.sh --yes      provision, without the application (its registry does not exist yet)
publish.sh           build the image into that registry
deploy.sh --yes      deploy the application
bootstrap.sh         the database role and the extensions
migrate.sh           the schema
measure.sh           a real token, an authenticated call, a document, a question
destroy.sh --yes     always, whatever happened above
```

Three lines in that file are load-bearing in a way a diff does not show, so
`scripts/check.sh` asserts all three rather than trusting them:

- **The teardown is `if: always()`.** `success()` would leave a failed run's database
  billing until somebody noticed — and the runs that fail are precisely the ones nobody is
  watching.
- **`cancel-in-progress` is `false`.** Cancelling a run mid-flight kills the job between the
  deployment and the teardown, which is the same expensive failure with a person's finger on
  it.
- **`permissions: id-token: write`.** Without it there is no OIDC token and `azure/login`
  fails on an empty assertion.

The environment is named `ci<run number>`, which is short on purpose: every resource name is
derived from it, and the tight Azure limits are unforgiving — a PostgreSQL role name stops at
63 characters, and that has already cost this project a deployment.

What `measure.sh` records is uploaded as an artifact before the teardown, because the
environment stops existing a step later.

### The subject GitHub actually presents

The first three runs of this pipeline failed on an invented action version, and the fourth
failed on something worth keeping:

```
AADSTS700213: No matching federated identity record found for presented assertion subject
'repo:sergiomedi@100800516/paimon@1353634150:ref:refs/heads/main'
```

That is not the subject anybody writes down. Since **15 July 2026** every new repository — and
every repository renamed or transferred after that date — presents an **immutable** subject
claim, with the numeric ids of the owner and the repository embedded in it, rather than
`repo:<owner>/<repo>:ref:refs/heads/main`.

The reason is worth understanding rather than working around: a name can be given up and taken
by somebody else, and a credential that trusted a name would follow it to its new owner. An id
cannot be re-registered.

`federate.sh` therefore reads the ids from the GitHub API rather than constructing the subject
from the names it was given, and federates both spellings — a repository older than that date
and not opted in still presents the legacy one, and nothing outside the repository can tell
which without guessing at a date. The script then prints how to delete the pair that went
unused, because the legacy subject trusts a name and the immutable one does not.

**The error message is the only place the presented subject appears.** If this ever fails
again, read the subject out of the Azure login step's log and federate exactly that string.

### A token for something that is not a person

The pipeline signs in as a service principal, and that needs one more thing than a person
does. **Client credentials are not delegated**: Entra issues no token at all for a resource
the calling application holds no *app role* on, and reports that the application is not
assigned to a role — which sounds like an Azure RBAC problem and is not one. The delegated
scope and the pre-authorised Azure CLI from
[ADR-0043](adr/0043-a-person-gets-a-token-the-same-way-a-workload-does.md) cover somebody at
a terminal and do nothing here.

So the API's registration exposes an application role, `Deployment.Verify`, with
`allowedMemberTypes: ["Application"]` — deliberately not `User`, because a role that also
admitted people would be a different authorisation decision wearing the same name. Defining
it is `token.sh`'s job; assigning it is `federate.sh`'s, because an assignment lives on the
service principal rather than on the registration.

The pipeline is **not** given rights over that registration. It may deploy resources; it may
not rewrite the application everything authenticates against. `measure.sh` therefore uses the
registration and never prepares it, and says so when the preparation is missing rather than
attempting it.

## How a release moves, and how it is undone

> **This has not run.** Everything from here to the end of the next section describes
> scripts and a workflow that exist, compile, and are enforced by the gates — and that have
> never once been executed against Azure. The verification workflow was in exactly this
> state for nine runs, and each of those nine found something no gate could: an action
> version that did not exist, a subject claim nobody writes down, a federated credential
> matched by the wrong field, an OIDC assertion five minutes dead. There is no reason to
> think this half is different, and the paragraphs below are written in the present tense
> because that is how the design reads, not because it has been observed.

```bash
./scripts/azure/release.sh      # the newest published image, checked before it serves
./scripts/azure/rollback.sh     # back to the previous revision, in seconds
```

The application runs in **multiple active revisions** mode. `release.sh` deploys the image as
a revision that takes **no traffic**, labels it `green`, waits for readiness *through that
label's own hostname* — `ca-paimon-api-<env>---green.<domain>`, and the three dashes are not a
typo — and only then shifts weight to it. Readiness rather than liveness on purpose: it opens
the database, the cache and the model endpoint, so what is checked is that the revision can
serve rather than merely start.

If the check fails, nothing moves. The new revision sits there idle, costing nothing while it
serves nothing, and the script prints the command that shows its logs.

`rollback.sh` changes one weight back. No build, no deployment, no image to find: the previous
revision is still running and was serving every request a few minutes ago. It deliberately
checks nothing first — a rollback is run when something is *already* wrong, and a script that
pauses to verify the version that worked ten minutes ago is a script that makes an outage
longer.

### A deployment is not a release

This distinction is enforced rather than described. `deploy.sh` reads which revision is
serving and passes it back into the template, so an ordinary deployment — a setting, a scale
limit, a template fix — leaves the traffic exactly where it was. Without that, every
deployment would hand traffic to the newest revision as a side effect: a release nobody asked
for, and a rollback nobody noticed.

Revisions are named after the commit their image was built from, because "which revision is
serving" should be answerable rather than guessable. Container Apps would otherwise generate a
suffix that is unique, meaningless, and impossible to ask for by name.

## Promotion, and the gate in front of it

```
Actions → Promote → Run workflow → target: prod
```

`.github/workflows/promote.yml` is the only thing here that leaves an environment running,
and it is deliberately not triggered by a merge. It provisions, publishes, bootstraps,
**migrates, and then releases** — in that order, which is the ordering the expand-contract
rule exists to make safe. Between those last two steps the schema is ahead of the code that
is serving, which is exactly the window a backward-incompatible migration would break, with
nothing deployed yet to blame.

`scripts/check.sh` asserts that ordering, along with the other two properties this file must
have: that it names a GitHub Environment, and that it never runs `destroy.sh`. A promotion
that tore down what it had just released would be a very expensive copy-and-paste from the
verification workflow.

### Setting up the gate

The approval is a **GitHub Environment**, and it has to be created once in the repository —
Settings → Environments → New environment → `production`, with *Required reviewers* set to
yourself. Until that exists the workflow runs unreviewed, which is the one part of this
phase a file in the repository cannot enforce.

That name matters twice. It is the gate, and it is also the subject the federated credential
was created for: `repo:<owner>/<repo>:environment:production`. A run that somehow started
without the approval could not obtain a token either, because Entra matches the subject
exactly.

**The GitHub Environment and the Azure environment are different things with confusable
names.** `production` is the gate; `target` is the Azure environment — the resource group
suffix — and it defaults to `prod`. One approval mechanism can gate promotions to several
Azure environments, which is why they are not the same field.

## The rule about migrations

Blue-green means two versions of the code run against **one database** for as long as the
switch takes. That is not a side effect to tolerate; it is the reason for the rule:

> A migration may add. It must not remove or rename anything the running code still uses.

Which is the expand–migrate–contract pattern, and it spreads a breaking change across
releases rather than across a maintenance window: add the new column, deploy code that
writes both, backfill, deploy code that reads the new one, stop writing the old one, and
only then drop it. Each step is safe with either version running.

The practical consequence in this repository: migrations run as their own job, against the
environment, **before** the new revision takes traffic ([ADR-0040](adr/0040-migrations-run-from-inside-the-network.md)),
and `alembic downgrade` is not a rollback plan. A release is reverted by weight; the schema
is not reverted at all.

## What it actually took, measured

Run 10, the first that reached the end. Every figure is from the run's own artifact rather
than from an estimate — `docs/measurements/<env>-deploy.log`, written by `deploy.sh` while
it waited, and uploaded whether the job passes, fails or is killed.

| | |
|---|---|
| The whole job | 27m 46s |
| Provisioning, first pass | 12m — the Container Apps environment for 4, then PostgreSQL for 6 |
| Build and push the image | ~2m, on the runner |
| Provisioning, second pass | 4m, adding the application and its two jobs |
| Bootstrap, migration | under a minute each |
| First request, warm | 0.423s |
| Unauthenticated call | 401, which is the expected answer |
| Authenticated call | 0.582s |
| Ingesting a document | 1.098s, 6 chunks embedded and written over the private endpoint |
| First grounded answer | 1.609s, 1 citation, 6 chunks retrieved, 656 tokens |
| The same question again | 1.280s — an embedding cache, not an answer cache ([ADR-0039](adr/0039-the-cache-runs-beside-the-thing-that-uses-it.md)) |
| Teardown | seconds, and then ARM's own time in the background |

**What all of it cost: 1.48 EUR.** That is the whole subscription for the period — Phase 7's
deployed-and-measured afternoon plus twelve delivery runs, each of which provisioned a
managed PostgreSQL server, a vector search service, two model deployments, a container
registry, a Container Apps environment and a Log Analytics workspace, exercised them, and
deleted them. Per-service figures are in the measurement file, filled in by hand from Cost
Management, because the invoice is the only authority on what something cost.

The number is [ADR-0044](adr/0044-delivery-without-a-standing-environment.md) collected. A
standing environment for the same period would have been roughly six euros a day.

Two of those numbers are worth more than the rest.

**PostgreSQL is six of the twelve minutes**, and it is six minutes of one resource that
everything else waits for. That is the cost of a private network with no public address
([ADR-0038](adr/0038-a-database-with-no-password-and-no-public-address.md)) and it is not recoverable by
making anything else faster.

**The teardown takes seconds**, which it did not until run 9 measured it taking
thirty-three minutes — more than the rest of the pipeline put together. A key vault and an
Azure OpenAI account are soft-deleted rather than deleted, and purging one cannot happen
until the resource group delete has completed. So the purges waited on a PostgreSQL server
being deleted, and a run killed by its own timeout never reached them: the soft-deleted
account it left behind then held the subscription's only Azure OpenAI slot, and failed the
*next* run at preflight. Deleting those two by name takes seconds, so they go first now and
the group follows without being waited on. The ordering was never about speed.

## What this pipeline will not tell you

Stated here rather than left to be assumed:

- **Nothing that needs time.** An environment that lives for eight minutes cannot observe
  certificate renewal, log retention, a slow leak, or a migration meeting a table with a
  million rows in it.
- **Nothing about load.** One replica, no concurrency, fifteen requests.
- **Nothing about data.** Every run starts empty, so no migration is ever tested against
  real data and no query is ever tested against a table that is not tiny.
- **Nothing about a second region.** There is one, and failover is not modelled.
