# Postmortem: INC-3187, payments pool saturation

Sample material written for this repository's benchmark. Not a description of any
real system.

## Summary

Payment authorisation failed for 11 minutes on 2026-08-14. The payments service
exhausted its database connection pool for the second time in eighteen months.
The first occurrence was INC-2451.

## What happened

The nightly reconciliation job was re-enabled after the capacity review signed it
off. It ran against the dedicated pool that INC-2451's actions created, and that
pool was correctly sized. The saturation was not the job.

A deployment three hours earlier had raised the payments service replica count
from four to nine. Each replica opens its own pool, and the per-service
connection ceiling is enforced at the database, not at the service — so nine
replicas asking for their configured pool size exceeded the ceiling and the
ninth replica's connections were refused.

## What the on-call engineer did

The engineer followed **RB-114**, which is the procedure for a saturated pool,
and did not improvise. Step three of that runbook is what resolved this
incident. The engineer did not need to restart anything.

This postmortem deliberately does not restate RB-114's steps. A procedure copied
into a postmortem is a procedure that stops being updated, and the copy is what
somebody finds eighteen months later.

## What went wrong

Replica count and connection ceiling are owned by different teams and reviewed in
different meetings. Nothing in the deployment pipeline compares the two.

The alert that fired was on authorisation failure rate. Pool utilisation was at
100 per cent for four minutes before any customer was affected, and the 80 per
cent alert that INC-2451 added had been routed to a channel nobody reads.

## Actions

- The deployment pipeline now refuses a replica count that would exceed the
  ceiling. The current ceiling is recorded in the service limits reference, not
  in this document, for the reason given above.
- The 80 per cent pool utilisation alert now pages the primary on-call.
- RB-114 gained a fourth step covering replica-count changes.
