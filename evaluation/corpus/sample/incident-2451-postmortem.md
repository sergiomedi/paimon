# Postmortem: INC-2451, checkout latency

Sample material written for this repository's benchmark. Not a description of any
real system.

## Summary

Checkout p99 latency rose from 240 ms to 9 s for 47 minutes. No requests were
lost. The cause was connection pool exhaustion in the payments service, triggered
by a background reconciliation job that opened one connection per merchant and
held it for the duration of its run.

## Timeline

The reconciliation job started at 02:00 UTC on its usual schedule. Latency alerts
fired at 02:14 once the pool was fully consumed. The on-call engineer restarted
the payments service at 02:41, which cleared the pool and restored latency
immediately. The job was disabled at 02:47 and the incident closed at 03:01.

## What went wrong

The reconciliation job had run nightly for eight months without incident. A
merchant onboarding campaign had raised the merchant count past the pool size for
the first time, so the job's connection usage crossed a threshold nobody had
written down.

Restarting the service cleared the symptom but not the cause. Had the job been
scheduled to retry, the incident would have recurred within the hour.

## What went right

The alert fired on user-visible latency rather than on pool utilization, so it
described the impact rather than a proxy for it. The on-call engineer found the
correct runbook in under three minutes.

## Actions

The reconciliation job now uses a dedicated connection pool sized independently of
the request pool, which is the same separation the platform applies to agent
workloads. Pool saturation is now alerted at 80 per cent, and merchant count is
part of the capacity review.
