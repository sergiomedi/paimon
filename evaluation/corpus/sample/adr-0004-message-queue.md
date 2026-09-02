# ADR-0004: Use a message queue for outbound webhooks

Sample material written for this repository's benchmark. Not a description of any
real system.

## Status

Accepted.

## Context

Outbound webhooks were delivered inline, inside the request that produced the
event. A slow customer endpoint therefore held one of our request workers for its
entire timeout, and a customer whose endpoint was down could consume a meaningful
share of our capacity by doing nothing at all.

Retries were attempted in the same request, which meant a delivery either
succeeded within one request's lifetime or was lost.

## Decision

Webhook deliveries are enqueued and delivered by a separate worker pool.

The queue is durable, so a delivery survives a deploy. Retries use exponential
backoff with jitter, capped at twenty-four hours, after which the delivery is
moved to a dead letter queue and the customer is notified by email.

## Consequences

Delivery is now asynchronous, so the API can no longer report whether a webhook
arrived. Customers who relied on that behaviour must poll the deliveries endpoint
instead.

A slow customer endpoint now consumes a worker from the delivery pool rather than
from the request pool, so it degrades webhook delivery for everyone but leaves the
API unaffected. That is a better failure mode, not an absent one.
