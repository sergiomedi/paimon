# ADR-0039: The cache runs beside the thing that uses it

- **Status:** Accepted
- **Date:** 2026-09-11
- **Phase:** 7 — Cloud

## Context and problem statement

The application requires Redis. It will not start without one, because readiness
reports on it and the settings make it mandatory. So deploying the application
means deploying a Redis, and the obvious answer — Azure Cache for Redis — costs
money, takes time to create, and adds a managed service to an environment whose
whole design is that it can be destroyed in one command.

The question is what Redis is actually holding here. The answer, written down in
the settings module long before this deployment existed, is: an embedding cache,
rate-limit counters and agent checkpoints, none of which is a system of record.
Eviction is an accepted outcome. Nothing in Redis, if it vanished, would be lost
— it would be recomputed.

That changes the question from "which Redis service" to "does this need to be a
service at all".

## Decision drivers

- Everything in it is derivable. The module docstring says so, and it predates
  this decision.
- The environment is created and destroyed repeatedly (ADR-0036). Anything with a
  long creation time taxes every cycle.
- The budget for the whole phase is smaller than a month of the database.
- A managed cache reachable over the network needs a credential, and this platform
  has spent three ADRs removing credentials.

## Considered options

1. **Azure Cache for Redis.** The default answer and the one an architecture
   diagram expects. It is a real service with persistence, replication and a
   support contract — and an access key, a provisioning wait measured in tens of
   minutes, and a bill that runs while the environment sits idle between
   measurements.
2. **A Redis container as its own Container App**, with internal TCP ingress.
   Shared across replicas, which is the property a rate limiter actually wants.
   But internal ingress in a VNet-integrated environment is reachable from the
   whole virtual network, so it either gets a password — the first stored
   credential in the system, protecting data that is by definition recomputable —
   or it does not, and the answer to "is that Redis authenticated" is "no".
3. **A Redis container in the same Container App**, beside the process that uses
   it.

## Decision

Option 3. Redis runs as a second container in the API's replica, reachable at
`127.0.0.1:6379` and from nowhere else.

```bicep
{
  name: 'cache'
  image: 'docker.io/library/redis:7.4-alpine'
  command: ['redis-server']
  args: ['--maxmemory', '256mb', '--maxmemory-policy', 'allkeys-lru',
         '--save', '', '--appendonly', 'no']
}
```

It has no ingress, so it has no address outside the replica, so there is nothing
to authenticate to and no credential to store. It is bounded at 256 MB and
evicts least-recently-used keys when it fills, because a cache that refuses
writes when full is an outage wearing a cache's clothing. Persistence is off in
both forms: there is nothing here worth surviving a restart, and writing it to
disk buys slower writes and a corrupt file to recover.

## Consequences

**Positive.** It costs nothing beyond the CPU and memory already allocated to the
replica. It starts in under a second, so it costs the create-and-destroy cycle
nothing. It has no credential, which keeps the `secrets: []` in the container app
true. And its failure mode is bounded: if it dies, the replica's liveness probe
notices and the replica is replaced, which is the correct response to losing a
cache.

**Negative — the cache is per-replica.** Three replicas hold three caches, so
the hit rate falls as the app scales out and the same embedding may be computed
once per replica. That is a cost in tokens rather than a correctness problem,
and at this size it is small; at a size where it is not, the cache is worth
paying for.

**Negative — anything that must be shared cannot live here.** A rate limiter
counting per replica does not limit the rate; it limits it three times, badly.
Nothing currently rate-limits through Redis, so nothing is broken today — but
this is the decision that has to be revisited the moment something does, and it
is worth naming the trigger rather than discovering it.

**The trigger for changing this.** Redis becomes a managed service when
something in it stops being derivable: a rate limit that must hold across
replicas, a session, a queue, a lock, or agent checkpoints that must survive the
replica that made them. Any one of those makes it a system of record, and a
system of record does not belong in a sidecar with persistence switched off. Until
then, this is the smallest arrangement that is honest about what the cache
contains.

**On what this says in an interview.** The weaker version of this decision is
"I used a sidecar because it was cheaper". The actual argument is that the
contents were already documented as expendable, and the deployment was made to
match the documentation rather than the other way round — which is also why the
trigger above is specific enough to act on.
