# Service limits reference

Sample material written for this repository's benchmark. Not a description of any
real system.

This is the single place the numbers live. Procedures reference it rather than
restating it, because a number written in two places is a number that disagrees
with itself within a quarter.

## Database connections

The per-service connection ceiling is **400**. It is enforced at the database by
a role-level limit, so a service that exceeds it has its connections refused
rather than queued.

The default pool size per replica is 40. The ceiling is therefore reached at ten
replicas, and the deployment pipeline refuses a replica count above nine for any
service using the default pool size.

Raising the ceiling requires the capacity review, because it is shared across
every service on the instance. The instance-wide maximum is 2000.

## Search API

Sixty search requests per minute per tenant, as documented in the search API
reference. That limit is unrelated to the connection ceiling above and is
enforced at the gateway.

## Background jobs

The reconciliation job has a dedicated pool of 25 connections, sized
independently of the request pool after INC-2451. It is not counted against the
per-replica default, but it *is* counted against the per-service ceiling of 400.

## Retention

Agent run records are kept for 90 days. Telemetry is kept for 30. Neither is
configurable per tenant.
