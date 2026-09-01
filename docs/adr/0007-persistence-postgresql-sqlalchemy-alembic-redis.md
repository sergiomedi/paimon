# ADR-0007: Persistence — PostgreSQL, SQLAlchemy, Alembic, Redis

- **Status:** Accepted
- **Date:** 2026-09-01
- **Phase:** 1 — Foundation

## Context and problem statement

The platform persists several categories of state with genuinely different access patterns:
document and chunk metadata, user and tenant records, conversation history, agent execution
state, and — depending on the retrieval backend selected under
[ADR-0003](0003-ports-and-adapters-for-llm-and-vector-store.md) — vector embeddings.

It also needs ephemeral state: cached embeddings, rate-limit counters, and from Phase 3 the
checkpoints of long-running LangGraph executions.

The decisions to make now are which engines, how schema changes are managed, and how the
connection pool is sized — the last because agent workloads hold connections for far longer
than HTTP requests do, and a pool sized for the latter fails under the former.

## Decision drivers

- Transactional integrity for document ingestion and agent state.
- Schema evolution must be versioned, reviewable and reversible.
- Vector search must be available locally at zero cost.
- Async throughout, to match FastAPI's concurrency model.

## Decision

**PostgreSQL 17** as the system of record, with the **pgvector** extension enabled. One
engine covering relational data and local vector search removes an entire component from
the development environment.

**SQLAlchemy 2.0** in fully async mode with `asyncpg`, used through the repository pattern.
ORM models live in `infrastructure/persistence/models/` and never leave it: use cases
receive domain entities, and mapping happens at the repository boundary. This is what stops
the database schema from becoming the domain model by default.

**Alembic** for migrations from the first table. `Base.metadata.create_all()` is never used
outside test fixtures — it produces a schema with no history, no review and no path forward.
Migrations are reviewed like code and applied as an explicit deployment step.

**Redis 7** for embedding cache, rate-limit counters and, from Phase 3, LangGraph
checkpoints. Everything in Redis is derivable or expendable: it is a cache and a coordination
point, never a system of record.

**Connection pool sizing is treated as a design parameter, not a default.** Two pools are
configured separately — a request pool for HTTP handlers, and a smaller dedicated pool for
agent execution. Agent graphs can hold a connection for minutes; sharing one pool means a
handful of concurrent agent runs can starve the API entirely. The pools are sized from a
documented model (expected concurrency, mean hold time, PostgreSQL `max_connections`) and
that model is recorded alongside the configuration rather than left implicit.

## Consequences

### Positive

- One database engine to run, back up and reason about during development.
- pgvector makes retrieval work offline and at zero cost, and gives Phase 6 a second
  retrieval backend to benchmark against.
- Versioned migrations make schema history reviewable and deployments reversible.
- Separated pools contain agent-workload pressure instead of letting it reach the API.

### Negative

- pgvector does not match Azure AI Search on hybrid ranking or semantic reranking at scale.
  Accepted: it is the development and comparison backend, not the production target.
- Async SQLAlchemy has sharper edges than the sync API — lazy loading in particular must be
  avoided explicitly. Mitigated by eager-loading strategies at the repository boundary and
  by a lint rule against implicit IO.
- Two pools mean two things to tune and two failure modes to understand. Accepted; the
  alternative failure mode is worse and harder to diagnose.

### Neutral

- Putting agent checkpoints in Redis makes them subject to eviction. Runs that must survive
  restarts will need PostgreSQL-backed checkpointing; the decision point arrives in Phase 3.

## Alternatives in detail

### A dedicated vector database (Qdrant, Weaviate, Milvus)

Better vector performance and richer filtering than pgvector, and a reasonable production
choice at scale. Rejected for the development backend because it adds a service to the
compose stack and a second consistency boundary, to solve a problem the project does not yet
have. The production target is Azure AI Search regardless, so the local backend's job is
correctness and cost, not peak throughput.

### MongoDB or another document store

Attractive for heterogeneous document metadata. Rejected: ingestion, tenancy and agent state
all want multi-record transactional guarantees, and PostgreSQL's `JSONB` covers the
schema-flexible portion without giving them up.

### Migrations via `create_all` in development, Alembic later

Faster on day one. Rejected because the migration that reconstructs history retroactively is
never written, and the first production deployment then has no reliable path from an empty
database to the current schema.
