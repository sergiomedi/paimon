# ADR-0017: Persist agent runs on the drivers the platform already has

- **Status:** Accepted
- **Date:** 2026-09-03
- **Phase:** 3 — Agents

## Context and problem statement

The phase plan anticipated a decision: LangGraph ships `langgraph-checkpoint-postgres`, which
depends on **psycopg 3**, while this platform runs on **SQLAlchemy with asyncpg**. Adopting it
would put two PostgreSQL drivers and two connection pools in one process against one database,
and the plan recorded that as the likely cost of durable agent state.

Writing the batch showed the premise was wrong, and the correction is worth recording because
it is the kind of dependency that gets adopted by assumption.

There are two different things called a checkpoint here.

`AgentCheckpointer` is this platform's own port. It persists a run as the domain describes one:
named steps, timezone-aware timestamps, per-step token counts, and a status — including
`AWAITING_INPUT` — that an operator acts on. It exists so a run can be listed, inspected and
attributed after the fact.

LangGraph's checkpointer persists *graph state* so a suspended execution can resume at the node
it stopped at. It is the machinery behind `interrupt`, and its schema is the framework's, not
the domain's.

Only the second requires the framework's package, and nothing in this batch resumes anything.

## Decision drivers

- A second driver is a second pool, a second set of timeouts and a second failure mode.
- Run records are read by people after an incident; they should be shaped by the domain rather
  than by whatever a framework happens to serialise.
- The dependency is genuinely needed the moment human-in-the-loop lands, and pretending
  otherwise would be as wrong as adopting it early.

## Considered options

1. **Implement `AgentCheckpointer` on SQLAlchemy and asyncpg.** No new dependency now.
2. **Adopt `langgraph-checkpoint-postgres` now** and store run records inside it, or alongside.
3. **Reimplement LangGraph's checkpointer on asyncpg**, to avoid psycopg permanently.

## Decision

Option 1. `PostgresCheckpointer` is written on the drivers already in the project, against a
schema this platform owns: `agent_runs` keyed by thread, steps as one JSONB document, and one
index — tenant plus `started_at DESC` — because listing a tenant's recent runs is the only
query the table serves.

Steps are one document rather than a child table. They are written together, read together and
never queried individually, so a second table buys a join and nothing else. The cost is that a
step cannot be indexed alone, which is a query nothing makes.

`AgentMemory` lands in the same batch on `pgvector`, reusing the retrieval embedding model.
The same model deliberately: two models are two vector spaces, and a similarity computed
between them is a number with no meaning. Its namespace is a `text[]`, not a delimited string,
because a delimiter is a character that eventually appears inside a segment, and on that day
the boundary moves without an error.

Option 3 is rejected in advance: when `interrupt` arrives in the last batch of this phase, the
framework's own checkpointer will be adopted, psycopg with it, and the second pool accepted.
Reimplementing a resumption protocol to avoid a dependency is surface area traded for nothing.

## Consequences

**Positive.** No new driver this batch. Run records have the shape the domain gives them, so
the API can return them and an operator can read them without a translation nobody wrote.
`PostgresCheckpointer` passes the same ten contract assertions as the in-memory implementation,
so substituting one for the other is safe by test rather than by inspection.

**Negative.** When LangGraph's checkpointer does arrive, the process will hold two mechanisms
that both use the word checkpoint, and the distinction above will have to be legible in the
code or it will confuse whoever reads it next. The names are chosen with that in mind.

**Verified.** The migration was applied to a real PostgreSQL with pgvector and inspected: the
HNSW index with `vector_cosine_ops` and its build options, a `text[]` inside a composite
primary key, the descending index, the upsert on that key, and that a nested namespace does not
match its prefix.
