# Architecture Overview

> Status: living document. Updated at the end of every delivery phase.
> Last updated: Phase 1 — Foundation.

Paimon is an AI Operations Platform for engineering organizations. It turns
scattered operational knowledge — runbooks, postmortems, architecture decision
records, API documentation — into grounded answers and automated workflows.

This document describes the system at two levels of the [C4 model](https://c4model.com/):
system context and containers. Component-level detail lives in the module READMEs,
and every irreversible decision is recorded as an [ADR](../adr/README.md).

---

## 1. System context

```mermaid
flowchart TB
    engineer["Engineer<br/><i>SRE, developer, on-call</i>"]

    subgraph paimon["Paimon — AI Operations Platform"]
        core["Grounded answers, agent workflows<br/>and operational automation"]
    end

    aoai["Azure OpenAI<br/><i>chat + embedding models</i>"]
    search["Azure AI Search<br/><i>hybrid retrieval index</i>"]
    entra["Microsoft Entra ID<br/><i>identity provider</i>"]
    github["GitHub<br/><i>source of engineering knowledge</i>"]
    langfuse["Langfuse<br/><i>LLM tracing and cost</i>"]

    engineer -->|"asks questions, launches workflows"| paimon
    paimon -->|"completions, embeddings"| aoai
    paimon -->|"hybrid search over the corpus"| search
    paimon -->|"OIDC token validation"| entra
    paimon -->|"repositories, issues, docs"| github
    paimon -->|"traces, spans, cost"| langfuse
```

**Why this boundary.** Paimon owns orchestration, retrieval strategy, evaluation and
observability. It does not own the models, the identity store or the knowledge sources.
Every one of those is reached through an interface defined in the domain layer, so any
of them can be replaced without touching business logic (see [ADR-0003](../adr/0003-ports-and-adapters-for-llm-and-vector-store.md)).

---

## 2. Containers

```mermaid
flowchart TB
    engineer["Engineer"]

    subgraph platform["Paimon"]
        web["Web Application<br/><i>Next.js 16, TypeScript</i><br/>Chat, document management,<br/>evaluation dashboards"]
        api["API<br/><i>FastAPI, Python 3.13</i><br/>Use cases, RAG pipeline,<br/>agent runtime"]
        mcp["MCP Server<br/><i>Python</i><br/>Tools exposed to<br/>external MCP clients"]
        pg[("PostgreSQL 17<br/><i>+ pgvector</i><br/>Documents, users, agent state,<br/>local vector index")]
        redis[("Redis 7<br/>Embedding cache, rate limiting,<br/>agent checkpoints")]
    end

    aoai["Azure OpenAI"]
    search["Azure AI Search"]
    entra["Microsoft Entra ID"]
    langfuse["Langfuse"]

    engineer -->|HTTPS| web
    web -->|"JSON / SSE"| api
    api --> pg
    api --> redis
    api --> aoai
    api --> search
    api -->|"JWKS"| entra
    api --> langfuse
    mcp --> api
```

**Deployment note.** The agent runtime runs in-process with the API in Phases 1–6.
It is a separate *logical* container from day one — its own module with its own ports —
so extracting it into an independent service is a deployment change, not a rewrite.
That extraction becomes worthwhile when agent runs start starving the API's request
workers; the trigger is documented in [ADR-0007](../adr/0007-persistence-postgresql-sqlalchemy-alembic-redis.md).

---

## 3. Backend layering

Business logic must survive the frameworks around it. The backend is organized in four
layers with a strictly inward dependency direction.

```mermaid
flowchart RL
    interfaces["<b>interfaces/</b><br/>FastAPI routers, request/response<br/>schemas, dependency wiring"]
    application["<b>application/</b><br/>Use cases, orchestration,<br/>transaction boundaries"]
    domain["<b>domain/</b><br/>Entities, value objects,<br/><b>ports</b> (protocols)"]
    infrastructure["<b>infrastructure/</b><br/>Adapters: Azure OpenAI, Azure AI Search,<br/>SQLAlchemy, Redis, Entra ID"]

    interfaces --> application
    application --> domain
    infrastructure --> domain
```

| Layer | May import | Must never import |
|---|---|---|
| `domain` | stdlib, `pydantic` | anything else in the project, any framework |
| `application` | `domain` | `infrastructure`, `interfaces`, FastAPI |
| `infrastructure` | `domain` | `application`, `interfaces` |
| `interfaces` | `application`, `domain` | `infrastructure` (except at the composition root) |

The composition root — the single place where concrete adapters are bound to ports — is
`interfaces/api/dependencies.py`. It is the only module allowed to import from
`infrastructure`.

**This table is enforced, not aspirational.** `import-linter` runs in CI and fails the
build on any violation. See [ADR-0002](../adr/0002-monorepo-layout-and-module-boundaries.md).

---

## 4. Request flow: a grounded answer

```mermaid
sequenceDiagram
    participant U as Engineer
    participant W as Web App
    participant A as API (interfaces)
    participant UC as AnswerQuestion (application)
    participant R as Retriever (port)
    participant L as LLMProvider (port)
    participant O as Langfuse

    U->>W: question
    W->>A: POST /api/v1/chat (Bearer token)
    A->>A: validate JWT against JWKS
    A->>UC: execute(query, tenant)
    UC->>R: retrieve(query, k)
    R-->>UC: chunks + metadata
    UC->>L: complete(prompt, context)
    L-->>UC: answer tokens
    UC-->>A: answer + citations
    A-->>W: SSE stream
    UC->>O: trace (latency, tokens, cost)
```

Note that `AnswerQuestion` depends only on the `Retriever` and `LLMProvider` protocols.
Whether retrieval hits pgvector or Azure AI Search, and whether completion hits a local
model or Azure OpenAI, is decided at startup by configuration.

---

## 5. Non-functional posture

| Concern | Phase 1 position | Where it grows |
|---|---|---|
| **Scalability** | Stateless API, async I/O throughout, connection pooling sized explicitly | Phase 7: horizontal scaling on Container Apps |
| **Reliability** | Health and readiness probes, structured errors, no shared mutable state | Phase 5: SLOs on top of real telemetry |
| **Security** | OIDC token validation, secrets never in code, least-privilege DB roles | Phase 7: Azure Key Vault, managed identities |
| **Observability** | Structured JSON logs with correlation IDs from the first endpoint | Phase 5: OpenTelemetry traces + Langfuse |
| **Testability** | Ports make every external dependency substitutable; contract tests per port | Phase 6: automated evaluation pipeline |
| **Cost** | Local adapters keep development and CI at zero external spend | Phase 5: per-request cost attribution |

---

## 6. Known gaps

Recorded honestly rather than hidden — each is scheduled, not forgotten.

- **Multi-tenancy** is modelled in the domain (`tenant_id` on every aggregate) but not yet
  enforced at the database level. Row-level security lands with the RAG system in Phase 2.
- **The agent runtime shares the API process.** Acceptable while agent runs are short;
  revisited in Phase 3 when long-running graphs appear.
- **No rate limiting on LLM calls yet.** Redis is provisioned for it; the token-bucket
  implementation lands with the first real Azure OpenAI adapter.
