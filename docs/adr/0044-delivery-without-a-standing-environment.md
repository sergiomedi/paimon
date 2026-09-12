# ADR-0044: Delivery without a standing environment

- **Status:** Accepted
- **Date:** 2026-09-12
- **Phase:** 8 — Continuous Delivery

## Context and problem statement

Phase 7 proved the deployment works and then destroyed it, deliberately
([ADR-0036](0036-an-environment-built-to-be-destroyed.md)). Phase 8 has to deliver
continuously into that.

The two are in direct tension, and pretending otherwise would produce a pipeline
that reads well and cannot be run. Continuous delivery is normally defined
against an environment that exists: you deploy to it on every merge, it stays up,
and the next deploy replaces what is running. This platform's environment costs
roughly six euros a day for a database nobody is querying, out of a fixed amount
of trial credit, and the previous phase's central argument is that it should not
exist when nobody is measuring it.

There is a second tension, smaller and sharper. Phase 7 found four defects by
deploying — including two adapters that had been wrong since Phase 1 and passed
every test because their stand-ins agreed with them. A pipeline that only lints
templates and builds an image would not have caught any of them. Whatever this
phase builds has to *run the thing*, against real services, or it repeats the
mistake this project has just spent a phase paying for.

## Decision drivers

- The pipeline has to exercise the real chain — deploy, bootstrap, migrate,
  authenticate, answer — or it verifies nothing that matters.
- Nothing may be left billing by a pipeline that succeeded, or by one that failed.
- No long-lived cloud credential in GitHub. Phase 7 removed every password from
  the platform ([ADR-0037](0037-keyless-is-enforced-at-the-resource.md),
  [ADR-0038](0038-a-database-with-no-password-and-no-public-address.md)); a
  client secret in a repository secret would put one back at the front door.
- A rollback has to be faster than a rebuild. The slowest possible moment to
  discover that reverting means twelve minutes of CI is the moment production is
  broken.
- Schema and code are deployed by the same pipeline and must not have to move
  together.

## Considered options

1. **A permanent `dev` environment, deployed on every merge.** The textbook
   shape, and what a funded team does. Here it is about six euros a day for a
   database that is queried when somebody remembers to, against a credit balance
   that is the budget for the whole project. It also contradicts ADR-0036 six
   days after that decision was made on the same grounds.
2. **Manual deployment only, from a workflow dispatch.** Cheap and honest, and it
   leaves the word *continuous* doing no work: nothing is verified until somebody
   chooses to verify it, which is the state Phase 7 was already in.
3. **An ephemeral environment per pipeline run**: created, exercised end to end,
   and destroyed in the same job, with the teardown guaranteed to run whether the
   verification passed or failed.

## Decision

**Option 3, and the release itself is separate from the verification.**

A merge to `main` builds an image, then creates a complete environment under a
run-specific name, bootstraps it, migrates it, obtains a token, ingests a
document, asks a question about it, and destroys the environment — in a job whose
teardown runs on failure, on cancellation, and on success. What it verifies is
what Phase 7 verified by hand, and what would have caught every one of that
phase's four defects: settings the container actually accepts, a token the API
actually accepts, an index the service actually accepts, a schema the database
actually accepts.

Promotion to a real environment is a **separate, gated workflow**, because
whether a deployment exists is a spending decision and spending decisions are not
made by a merge. It uses a GitHub Environment with a required reviewer, which is
what turns "the pipeline can deploy" into "a person decided to".

**Authentication is OpenID Connect**, with federated credentials on an app
registration and no secret anywhere. GitHub mints a short-lived token per run,
Entra trusts it for a specific repository, branch and environment, and there is
nothing in the repository to leak or rotate. The subject claim is scoped per
trigger, so a pull request cannot obtain the credential that deploys, and the
production environment's credential exists only for that environment.

**Releases are blue-green, by revision label.** The application moves to
`activeRevisionsMode: multiple`: a new revision is created with **no traffic**,
receives a `green` label and its own hostname, is verified through that hostname,
and only then takes traffic by weight. Rollback is the same command with the
weights exchanged — seconds, no rebuild, and the previous revision is still
running. `api.bicep` has said since Phase 7 that this is "a delivery concern with
a phase of its own"; this is that phase.

**The schema moves first, and separately.** Migrations run as their own job
against the environment *before* the new revision takes traffic, and every
migration must be backward compatible with the revision currently serving —
expand, migrate, contract, in that order and usually in that many releases. The
rule this produces is short enough to remember: **a migration may add, and must
not remove or rename anything the running code still uses.** A rollback that has
to undo a migration is not a rollback.

## Consequences

**Positive.** Every merge exercises the entire chain against real Azure services
rather than against stand-ins, which is the one form of testing this project has
learned it cannot skip. Nothing is left running: the environment's lifetime is
the job's. There is no cloud credential in GitHub to steal. A bad release is
reverted in seconds by weight rather than in minutes by rebuild, and the schema
does not have to be reverted at all.

**Negative — a pipeline run costs money and takes minutes.** About twelve minutes
and a few cents of tokens per merge, plus the provisioning time of a database
that exists for eight of those minutes. That is the price of verifying against
the real thing, and it is the reason the verification runs on `main` rather than
on every push to every branch.

**Negative — an ephemeral environment cannot verify everything a standing one
can.** Nothing that only appears after hours of uptime, nothing about
accumulated data, nothing about a migration meeting a table with rows in it. The
pipeline proves the deployment path and the contracts, not the passage of time.

**Negative — blue-green doubles the replicas briefly**, and with a database
behind both revisions it means two versions of the code against one schema at
once. That is not a side effect to tolerate; it is precisely why the
backward-compatibility rule above is a rule rather than a preference.

**Negative — the pipeline needs rights the application does not.** The template
creates role assignments, so the deploying identity needs to be able to create
them: Contributor plus Role Based Access Control Administrator at the
subscription. That is a broad grant, held by a federated credential rather than
by a secret, scoped to one repository and one branch, and it is the honest cost
of infrastructure-as-code that includes its own authorization model.
