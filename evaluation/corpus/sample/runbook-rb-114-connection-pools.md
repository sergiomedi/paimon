# RB-114: a saturated connection pool

Sample material written for this repository's benchmark. Not a description of any
real system.

## When to use this

The service is returning connection errors, or pool utilisation is at 100 per
cent. This runbook covers saturation only. If connections are being refused by
the database rather than exhausted in the service, that is a different symptom
and RB-119 covers it.

## Steps

1. Read the pool utilisation for every replica, not the aggregate. An aggregate
   at 70 per cent hides one replica at 100.

2. Check whether the replica count changed in the last six hours. Each replica
   opens its own pool, so the connections a service asks for are the pool size
   multiplied by the replica count.

3. **Compare that product against the per-service connection ceiling.** The
   ceiling is a database setting and is listed in the service limits reference.
   If the product exceeds it, reduce the replica count until it does not. This is
   the step that resolves the common case, and it resolves it without a restart.

4. If the replica count did not change, look for a client holding connections
   open. Do not restart the service first: a restart clears the symptom and
   destroys the evidence, and the incident recurs on the next run of whatever was
   holding them.

## What not to do

Do not raise the pool size to make the errors stop. The ceiling is per service,
so a larger pool per replica reaches it sooner, and the failure moves from one
replica to all of them at once.

Do not raise the ceiling without the capacity review. It is shared with every
other service on that database.

## Related

RB-119 covers refused connections. INC-2451 and INC-3187 are the two incidents
this runbook was written and revised for.
