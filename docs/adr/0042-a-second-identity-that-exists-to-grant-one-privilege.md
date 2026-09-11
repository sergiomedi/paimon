# ADR-0042: A second identity, which exists to grant one privilege

- **Status:** Accepted
- **Date:** 2026-09-11
- **Phase:** 7 — Cloud

## Context and problem statement

ADR-0038 gave the database no password and no public address, and accepted a
consequence in writing: *"a managed identity cannot log in until a role exists
for it inside PostgreSQL, and creating that role is SQL, not ARM. It is run once
per environment by the Entra administrator, from inside the network."*

That sentence has a hole in it, and the hole is the words *from inside the
network*. The administrator is a person with a laptop. The laptop is not inside
the network — that is the entire point of the previous decision — and nothing
else in the environment runs as that person. So the documented manual step could
not actually be performed by the person it was documented for, and the
environment could be deployed but not finished.

Two more things surfaced with it, both of which would have failed a first
deployment on their own:

- **`CREATE EXTENSION vector` fails on Azure** unless the extension is
  allow-listed in the server's `azure.extensions` parameter. The initial
  migration says `CREATE EXTENSION IF NOT EXISTS vector` and that is sufficient
  against the local `pgvector` image, which is precisely why the difference is
  the kind that is discovered in a deployment rather than in a test.
- **PostgreSQL 15 revoked `CREATE` on schema `public` from `PUBLIC`.** The
  workload role can therefore authenticate perfectly and still fail on its first
  `CREATE TABLE`, with a message about a schema rather than about a permission.

## Decision drivers

- The environment must be deployable end to end by one person, from outside it.
- ADR-0038's separation is worth keeping: the thing a role is created *for*
  should not be the thing that creates it.
- A step run by hand once per environment will be run twice by anybody unsure
  whether it ran at all.
- No new long-lived privilege on anything that serves traffic.

## Considered options

1. **Make the workload identity an Entra administrator of the server.** One
   identity, no job, no bootstrap: an Entra administrator's role is created by
   Azure, so it can sign in immediately and create its own tables. And the API
   then runs permanently as `azure_pg_admin`, with `CREATEROLE` and `CREATEDB`,
   which is exactly what ADR-0038 argued against. It would also mean superseding
   that ADR six days after accepting it, in exchange for saving one job.
2. **Keep it manual, and document a route that works** — obtain a token as
   yourself, `az containerapp exec` into a running replica, and run the SQL from
   inside the network from there. No new resources. It is also circular: the
   replica whose shell is needed is not ready precisely because the role does not
   exist yet, and it makes the one step between a deployed environment and a
   working one a sequence of six commands typed correctly under pressure.
3. **A second managed identity that exists only to hold this privilege**, used
   by a one-shot job.

## Decision

Option 3. `id-paimon-dbadmin-<env>` is registered as a second Microsoft Entra
administrator of the server, alongside the person deploying. A manually
triggered container apps job runs as it — the same image, a different command,
a different identity — and creates the workload's role, the extensions, and the
grants.

The separation survives: the workload identity is a plain database user that
cannot create roles, and the identity that can create them is attached to one
job and to nothing that serves traffic. What changes is only *who* holds the
administrator's privilege — an identity that exists for one job, rather than a
person who cannot reach the network.

The role is created **by object id**, with
`pgaadauth_create_principal_with_oid(name, oid, 'service', false, false)`. The
name-only form resolves a service principal by display name, which is not unique
in a tenant; the deployment guide's original SQL used it, and would have been a
latent ambiguity waiting for a second identity with a similar name.

The job also creates the extension, rather than leaving it to the migration.
Doing it here means the migration's own `CREATE EXTENSION IF NOT EXISTS` is a
no-op instead of a privilege failure, and the workload keeps no privilege it
does not need. And `azure.extensions` is set on the server in the template,
because without it nobody at all can create that extension.

Everything the job does is idempotent, and it reports what it found rather than
what it assumed: *role already exists* is a normal outcome, printed.

## Consequences

**Positive.** A new environment is now five commands with no manual SQL in the
middle, and none of them requires being inside the network. The failure that the
guide previously described as a manual step — and which accounted for the first
thing anybody would hit — is a job that either succeeds or says why. Two failures
that would each have stopped a first deployment cold, the extension allow-list
and the schema grant, are fixed before they happen rather than diagnosed
afterwards.

**Negative — there is now an identity with administrative rights on the
database.** It is narrow (one job, one execution per environment, no ingress, no
traffic) but it exists, and "no standing administrative credential" is no longer
quite true of this environment. The honest version is: no *password*, and one
identity whose only purpose is a privilege the workload must not have.

**Negative — a new ordering constraint.** `bootstrap.sh` comes before
`migrate.sh`, and a migration run first fails with an authentication error. Both
scripts name the other in their failure text, which is where the next person will
be looking.

**Negative — the bootstrap depends on Entra propagation.** The administrator
registration and the role assignment take minutes to become effective, so a
bootstrap run immediately after a deployment can fail on timing alone. It is safe
to retry, which is the property that makes that acceptable.

**A second program that has to be deployed to be tested.** It is unit-tested
against a fake connection — the order of its statements, its idempotency, and its
refusal to interpolate an identifier it has not checked — but PostgreSQL's
reaction to it is not something a test suite here can assert.
