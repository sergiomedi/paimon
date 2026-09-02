# ADR-0010: Separate embedding and chat ports

- **Status:** Accepted
- **Date:** 2026-09-02
- **Phase:** 2 — RAG
- **Refines:** [ADR-0003](0003-ports-and-adapters-for-llm-and-vector-store.md)

## Context and problem statement

[ADR-0003](0003-ports-and-adapters-for-llm-and-vector-store.md) established two ports,
`LLMProvider` and `VectorStore`, before either had a consumer. Phase 2 supplies the
consumers, and with them the information that decision lacked: the two things an "LLM
provider" does are used by entirely different parts of the system.

Ingestion embeds text. It never generates any. Answering generates text, and embeds only
the query. A single port forces the ingestion pipeline to depend on a text-generation
method it will never call, which means every test double for ingestion must implement
generation, and every future provider must supply both even when it does only one — a
constraint that would immediately exclude a dedicated embedding service.

The two also change for different reasons. Generation gains streaming, tool calling and
structured output; embedding gains batching and dimensionality control. Coupling them means
each set of changes churns an interface the other half does not care about.

## Decision drivers

- A consumer should depend on the methods it calls and no others.
- Test doubles should be cheap; a fake that must implement unused methods invites shortcuts.
- Dedicated embedding providers exist and should be usable without a chat model.
- Neither concern's evolution should force churn on the other.

## Considered options

1. **Two ports**: `EmbeddingModel` and `ChatModel`.
2. **One `LLMProvider` port** with both capabilities, as originally recorded.
3. **One port with optional methods**, raising `NotImplementedError` where unsupported.

## Decision

Two ports.

`EmbeddingModel` exposes `model_id`, `dimensions`, `embed_documents` and `embed_query`.
Document and query embedding are separate methods because several model families are
asymmetric — they expect an instruction prefix on the query side and none on the document
side — and routing both through one method quietly costs retrieval quality. A symmetric
model implements the two identically, at no cost.

`ChatModel` exposes `model_id` and `complete`, returning text together with token counts.
Usage is part of the contract rather than an optional extra: per-request cost attribution is
a Phase 5 deliverable, and a provider that does not report usage cannot be made to report it
after the fact.

The same phase also introduces `NativeHybridSearch` as a protocol a store may additionally
satisfy. That is the capability flag ADR-0003 called for, expressed as a type rather than a
boolean: a store either satisfies it or it does not, and the application selects its path
with a check the type checker understands.

## Consequences

### Positive

- Ingestion depends on embedding alone, and its test doubles implement one method.
- A dedicated embedding provider, or a local embedding model with no chat capability, is a
  first-class option rather than a special case.
- Each port carries its own contract suite, so an adapter is verified against exactly the
  behaviour it claims.

### Negative

- Two ports to configure and bind instead of one, and a provider that offers both is
  registered twice. Minor, and confined to the composition root.
- ADR-0003 now describes a port that does not exist under that name. Left uncorrected on
  purpose: ADRs record what was decided when it was decided, and rewriting one to match a
  later decision destroys the reasoning trail that gives it value.

### Neutral

- Streaming generation is not on `ChatModel` yet. It arrives with the endpoint that needs
  it, rather than being designed against an imagined consumer.

## Alternatives in detail

### Option 2 — Keep a single LLMProvider

Fewer moving parts, and defensible while both halves are always supplied by the same vendor.
Rejected because that assumption is exactly what the local adapter breaks: development runs
a local embedding model against a different local chat model, and a combined port makes that
ordinary configuration into an awkward composite object.

### Option 3 — One port with optional methods

Preserves the single name while allowing partial implementations. Rejected because it moves
a compile-time guarantee to runtime: the type checker would accept a call that fails on the
first request, and every consumer would need to defend against a method that may not work.
An interface whose methods might not be implemented is not an interface.
