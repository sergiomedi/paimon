# ADR-0040: Migrations run from inside the network, as a job

- **Status:** Accepted
- **Date:** 2026-09-11
- **Phase:** 7 — Cloud

## Context and problem statement

ADR-0038 gave the database no public address. That decision has a consequence
nobody deploys their way around: `alembic upgrade head` from a developer's
machine cannot reach the database, and never will. There is no firewall rule to
add, because public network access is off rather than filtered.

So the schema has to be changed by something already inside the virtual network.
The application is inside the network — but making the application migrate the
database on startup is a different decision with worse properties, and it is the
one this ADR mostly exists to refuse.

## Decision drivers

- The database is unreachable from outside. This is deliberate and not
  negotiable.
- A migration is a decision. Anything that runs it without one running it is a
  problem waiting for a bad migration.
- Three replicas starting at once must not run three migrations at once.
- Whatever migrates must be built from the same source as the thing that assumes
  the result.

## Considered options

1. **Migrate on application startup.** One fewer moving part, and the schema is
   never behind the code. It also means every cold start races every other cold
   start for a lock; a migration failure becomes a startup failure, so a bad
   migration takes the service down rather than being a job that failed; and a
   scale-out event runs schema changes. The convenience is real and the failure
   modes are the ones that happen at three in the morning.
2. **A jump host, or a temporary firewall opening, and migrate from a laptop.**
   This undoes ADR-0038 for the duration, in exchange for keeping a habit.
3. **A Container Apps job running the same image with a different command.**

## Decision

Option 3. A `Microsoft.App/jobs` resource with a **manual** trigger, running the
deployed image with `alembic upgrade head`, inside the same environment and
therefore inside the same network:

```bicep
configuration: {
  triggerType: 'Manual'
  replicaTimeout: 1800
  replicaRetryLimit: 0
}
```

Manual, not scheduled and not on-deploy: a migration stays a decision, and the
place to put a gate in front of it is a delivery pipeline, which Phase 8 is. No
retries — a migration that failed halfway is not improved by being run again
automatically, and the second attempt obscures the first one's error.

**The same image as the API, deliberately.** A migration run from a separately
built image is a migration run against a different definition of the schema, and
the difference shows up later as drift that nobody can attribute. That required
two changes to the image: it now carries `alembic.ini` and `migrations/`, and it
installs the `azure` extra — the latter because the migration authenticates the
same way the application does, which is to say with a token rather than a
password.

**And the migration had to learn to authenticate.** Alembic's `env.py` built a
URL and handed it to SQLAlchemy, which is enough with a password and not enough
with an Entra token: the token goes in the password field and has to be fetched
per connection (ADR-0038). So `env.py` now goes through the application's own
`build_engine` rather than constructing an engine of its own. A URL there would
have worked on every laptop and failed in every deployed environment — the worst
possible split, since the deployed database is the one a migration cannot be
rehearsed against.

## Consequences

**Positive.** Schema changes are deliberate, single-writer, and observable: a job
execution has a status, a duration and logs, which "the app restarted" does not.
The job costs nothing while it is not running. And the same mechanism is what
Phase 8 puts behind a gate, rather than something Phase 8 has to replace.

**Negative — a new environment needs three commands, not one.** Deploy, publish,
deploy, migrate. That is more than the single command this project has been
aiming at, and it is the honest cost of a database with no public address plus a
registry created by the same template that pulls from it.

**Negative — the schema can be behind the code.** Nothing enforces that the
migration ran, so a deployment whose migration was forgotten fails at the first
query rather than at startup. A readiness check comparing the schema revision to
the code's would close that, and it is worth doing when there is more than one
person deploying.

**Negative — it is one more thing that needs the manual PostgreSQL role.** The
job authenticates as the same managed identity as the application, so it fails
the same way before `pgaadauth_create_principal` has been run. `migrate.sh` says
so when the job fails, because that is the first thing it will be.
