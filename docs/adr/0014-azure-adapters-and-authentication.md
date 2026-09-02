# ADR-0014: Azure adapters and how they authenticate

- **Status:** Accepted
- **Date:** 2026-09-02
- **Phase:** 2 — RAG

## Context and problem statement

[ADR-0003](0003-ports-and-adapters-for-llm-and-vector-store.md) claimed that the platform
could run on Azure or locally behind the same ports. Until an Azure adapter existed, that was
an assertion. This decision covers the two questions building them raised: how much of the
local adapter can be reused, and how requests are authenticated.

## Decision drivers

- An adapter that duplicates the risky logic of another will diverge from it.
- Service keys in configuration are a liability; a deployment should be able to have none.
- A bearer token expires, and expiry must not look like a permissions failure.
- The ports must not acquire Azure-shaped methods to accommodate Azure.

## Decision

### Azure OpenAI reuses the parsing, not the addressing

Azure OpenAI speaks OpenAI's request and response shapes. What differs is that the URL names
a **deployment** rather than a model — a deployment can be called anything, so its name is
configuration and cannot be inferred — and that it carries an explicit `api-version`, pinned
here rather than left to the service, since a version change alters response shapes.

So the Azure adapters own addressing and authentication and share the response parsing with
the OpenAI-compatible ones. Writing a second full implementation would have duplicated the
part most likely to go wrong — ordering results by the index the provider reports, validating
the width — to avoid duplicating the part least likely to.

One Azure-specific behaviour is not shared: an empty message with a `finish_reason` of
`content_filter`. Reporting that as a generic empty answer sends the reader looking in the
wrong place, so the reason is surfaced.

### Azure AI Search is a genuine second implementation

It has its own schema, its own query language and a native hybrid ranker, so it satisfies
`NativeHybridSearch` and the application defers to it rather than fusing again. Azure fuses
with reciprocal rank at k=60, which is why the local backend uses the same constant
([ADR-0012](0012-fuse-retrieval-by-rank.md)): the two orderings are comparable rather than
merely both plausible.

Two details are worth recording because they are invisible until they bite. Azure permits
only letters, digits, underscore, dash and equals in a document key, while chunk ids contain a
colon; keys are therefore base64url of the tenant and chunk id together, rather than a
character substitution, which could map two different chunks onto one key and silently
overwrite one. And Azure reports per-document failures inside a `200` response, so the batch
result is inspected: a half-applied batch that returns success is how an index quietly loses
chunks nobody noticed were rejected.

Semantic ranking is exposed as a capability of this backend rather than applied silently.
pgvector has no equivalent, and ADR-0003 requires such differences to be visible.

### Both authentication mechanisms, selected by absence

`ApiKeyCredential` sends a static key. `EntraCredential` wraps any azure-identity credential,
so one code path covers a managed identity in Azure and a developer's `az login` locally, and
caches the token until shortly before it expires.

Which one is used is decided by whether a key is configured, not by a separate mode setting.
A deployment removes its keys by deleting them from configuration, rather than by also
remembering to flip a switch — one fewer way to end up with keys that are still valid and no
longer intended.

Headers are produced **per request**, not baked into a client at construction. A bearer token
expires, and a client built once with a token that has since lapsed fails in a way that reads
as a permissions problem rather than an expiry.

azure-identity is an optional extra. A deployment using service keys should not carry its
dependency tree, and the import is local to the function that needs it so the package remains
importable without it.

## Consequences

### Positive

- The Azure AI Search adapter passes the same `VectorStore` contract as pgvector, which is
  the first real evidence that the ADR-0003 abstraction holds.
- Keys can be removed from configuration entirely.
- Response parsing exists once, so the two OpenAI-shaped adapters cannot drift.
- The index schema is declared in code and can be reviewed and applied, rather than
  discovered from a failed write.

### Negative

- The Azure adapters are verified against an in-process stand-in for the service, not against
  Azure. That stand-in is a model of Azure written by the same person who wrote the adapter,
  so it can only find inconsistencies, never wrong assumptions. The contract class is
  parameterised so the same assertions run against a real search service the moment one
  exists; until then, the honest claim is that the adapter is internally consistent.
- Two authentication paths mean two failure modes to understand.
- `api-version` pinning means someone must bump it deliberately. That is the intent.

### Neutral

- Closing adapters is handled by a `Closeable` check at the composition root rather than by a
  method on the ports. Closing is a lifecycle concern of a particular implementation, and
  requiring it of every adapter would force an empty method onto the in-memory ones.

## Alternatives in detail

### Use the official `openai` and `azure-search-documents` SDKs

The conventional choice, and it would remove the request-shaping code. Rejected for three
reasons: both SDKs bring large dependency trees for a small amount of HTTP; their retry and
timeout behaviour is theirs rather than the platform's; and the local and Azure adapters
would then be written against different abstractions, which is exactly the divergence the
shared parsing exists to prevent. Worth revisiting if Azure adds a protocol feature that is
painful to implement by hand.

### API keys only

Simpler, and sufficient until Phase 7. Rejected because retrofitting managed identity means
touching every adapter and every deployment at once, whereas supporting it now costs one
class and an optional dependency.

### Entra ID only

The better posture, and where this should end up. Rejected as the sole option because it
requires role assignments before anything works at all, which would make the first Azure run
a permissions exercise rather than a retrieval one.
