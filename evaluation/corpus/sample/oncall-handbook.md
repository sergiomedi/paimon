# On-call handbook

Sample material written for this repository's benchmark. Not a description of any
real system.

## Rotation

The primary on-call engineer holds the pager for one week, starting Wednesday at
10:00 local time. Handover happens live, not over chat: the outgoing engineer
walks the incoming one through anything still open.

## Acknowledging a page

Acknowledge within five minutes. Acknowledging is not the same as fixing, and it
is not a commitment to fix alone — it means someone has eyes on the alert.

If a page is still unacknowledged after ten minutes it escalates to the secondary,
and after twenty to the engineering manager.

## Declaring an incident

Declare an incident when customer impact is confirmed or when you need more than
one person. Declaring early costs a channel and a status page entry. Declaring
late costs the timeline, because nobody was writing anything down.

## Severity

Severity one means the product is unusable for a substantial share of customers,
or data is at risk. Severity two means a major feature is degraded with a
workaround. Everything else is severity three and waits for business hours.

Severity is set by impact, not by cause. A database failover that nobody noticed
is not a severity one.

## Writing the postmortem

Every severity one and two gets a written postmortem within five working days.
Postmortems are blameless: they describe what the system allowed to happen, not
who was unlucky enough to be holding the pager.
