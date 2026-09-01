# ADR-0003: Ports and adapters for LLM and vector store

- **Status:** Accepted
- **Date:** 2026-09-01
- **Phase:** 1 — Foundation

## Context and problem statement

The target production stack is Azure OpenAI for generation and embeddings, and Azure AI
Search for retrieval. Two facts complicate depending on them directly.

**Availability.** Azure OpenAI requires an access approval step, and quota is granted per
model, per region. Development cannot be gated on an approval queue.

**Cost and capacity.** The free tier of Azure AI Search is limited to roughly 50 MB and
three indexes, with no service-level agreement. A continuous integration pipeline that
indexes documents on every run will exhaust it, and the paid tiers bill hourly whether the
index is queried or not — an unacceptable cost profile for a portfolio project that must
survive months of intermittent development.

Beyond both: a platform whose core value proposition is retrieval quality must be able to
*compare* retrieval backends. That is impossible if one of them is hardcoded.

## Decision drivers

- Development and CI must run at zero external spend and with no external dependency.
- The production target remains Azure; the abstraction must not water it down to a
  lowest-common-denominator feature set.
- Retrieval quality must be measurable across implementations (Phase 6).
- Framework and vendor independence is an explicit project constraint.

## Considered options

1. **Two ports in the domain layer** — `LLMProvider` and `VectorStore` — with a local
   adapter pair and an Azure adapter pair, selected by configuration.
2. **Direct dependency on the Azure SDKs** in the application layer.
3. **A third-party abstraction layer** (LangChain's `BaseChatModel` / `VectorStore`, or
   LiteLLM) used as the project's own interface.

## Decision

Option 1. The domain defines the protocols; infrastructure provides the adapters.

| Port | Development / CI adapter | Cloud adapter |
|---|---|---|
| `LLMProvider` | OpenAI-compatible local endpoint | Azure OpenAI |
| `VectorStore` | pgvector, in the PostgreSQL instance already running | Azure AI Search |

The ports are defined as `typing.Protocol` classes in `domain/ports/`, expressing only what
the application actually needs — not the union of what every backend can do. Adapters live
in `infrastructure/llm/` and `infrastructure/retrieval/`, and are bound at the composition
root from configuration.

Every port has a **contract test suite**: one set of behavioural tests that every adapter
must pass. This is what keeps the abstraction honest — without it, adapters drift and the
port becomes a lie told in two dialects.

**On not adopting a third-party abstraction as the project's interface.** LangChain and
LiteLLM are useful *inside* an adapter. Promoting either to the project's own boundary
inverts the dependency the architecture exists to protect: the domain would then depend on
a fast-moving third-party interface, and a breaking change upstream becomes a breaking
change in business logic. The port stays small and owned; LangChain may be used behind it.

## Consequences

### Positive

- Development, unit tests and integration tests run offline and free.
- Azure outages, quota exhaustion and approval delays stop being project blockers.
- Enables a genuine measured comparison of pgvector against Azure AI Search in Phase 6 —
  with hybrid-search quality, latency and cost per query as reportable numbers.
- Swapping in a different provider later is an adapter, not a migration.

### Negative

- Two implementations to maintain per port, and the contract suite must be kept meaningful.
  Real, recurring cost; accepted as the price of the three benefits above.
- **Leaky-abstraction risk is genuine.** Azure AI Search offers semantic ranking and
  built-in hybrid scoring that pgvector does not. The port must not be shrunk to the
  intersection of both. Mitigation: capability flags on the port, and features that only one
  backend supports are exposed explicitly rather than silently degraded.
- Behavioural differences between local and cloud models mean local tests do not prove
  production quality. Phase 6 evaluation runs against the Azure adapters.

### Neutral

- Configuration gains a backend-selection surface, which itself must be validated at startup
  rather than failing at first use.

## Alternatives in detail

### Option 2 — Direct SDK dependency

Fewer moving parts, and the fastest route to a working demo. Rejected because it violates
the project's dependency-inversion constraint, makes offline development impossible, makes
CI cost money, and forecloses the retrieval comparison that is one of the platform's most
credible outputs.

### Option 3 — Third-party abstraction as the project interface

Attractive: the work appears to be already done. Rejected because it relocates the
architectural boundary into a dependency the project does not control, and because these
interfaces are broad by design — a domain that depends on all of LangChain's surface is not
meaningfully decoupled from anything.
