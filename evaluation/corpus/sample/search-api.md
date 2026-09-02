# Search API

Sample material written for this repository's benchmark. Not a description of any
real system.

## Authentication

Every request requires a bearer token. Tokens are validated against the tenant's
key set; the service stores no credentials and issues no tokens of its own.

A request with no token and a request with an expired token both return 401 with
the same body. The reason is deliberate: telling a caller why a token was rejected
helps them construct a better one.

## Pagination

Results are paginated with an opaque cursor. Do not construct a cursor by hand and
do not assume it encodes an offset — it encodes a sort position, and offsets go
wrong the moment a document is inserted between two pages.

The default page size is twenty and the maximum is one hundred. Requests above the
maximum are clamped rather than rejected.

## Rate limits

Each tenant may make sixty search requests per minute. Exceeding the limit returns
429 with a Retry-After header in seconds. The limit is per tenant, not per token,
so issuing more tokens does not buy more capacity.

## Errors

A 503 from this endpoint means a dependency is unavailable and the request may
succeed if retried. A 500 means the request will not succeed as sent. Clients
should retry the former with backoff and log the latter.
