# ADR-0009: Dependency injection with FastAPI's Depends

- **Status:** Accepted
- **Date:** 2026-09-01
- **Phase:** 1 — Foundation

## Context and problem statement

The architecture of [ADR-0002](0002-monorepo-layout-and-module-boundaries.md) requires that
use cases depend on domain ports and never on the adapters implementing them. Something has
to bind the two, and where that binding lives determines whether the inversion is real or
merely drawn.

The mechanism also has to manage lifetimes. A database engine and a Redis client are
process-lifetime objects; a database session is request-lifetime; a use case is cheap and
can be built per request. Getting these wrong produces either connection leaks or a shared
session handed to concurrent requests.

## Decision drivers

- The binding must live in one identifiable place that can be enforced by a linter.
- Routers must receive use cases, not adapters.
- Request-scoped resources must be released deterministically, including on error.
- `mypy --strict` must be able to check the wiring.
- No dependency added unless it earns its place.

## Considered options

1. **FastAPI's native `Depends`**, with a composition root module.
2. **`dependency-injector`**: declarative containers with explicit scopes and wiring.
3. **A hand-rolled composition root** held on `app.state`, read directly by handlers.

## Decision

Option 1, with the composition root at `paimon/interfaces/api/dependencies.py`.

Process-lifetime resources are built once in an async context manager driven by the
application's lifespan, and exposed through providers. Providers assemble use cases from
those resources; routers annotate their parameters with the assembled use case or the
domain port, never with a concrete adapter.

The single-module rule is enforced rather than agreed: an import-linter contract forbids the
whole `interfaces` package from importing `infrastructure`, with an exception listing only
this module. A second composition root cannot appear without the build failing.

Three consequences of the choice are deliberate:

- **`Annotated` aliases** (`CurrentPrincipal`, `CheckReadinessDep`) keep the wiring readable
  at the call site and give mypy a concrete type to check.
- **Tests override providers**, not adapters. `app.dependency_overrides` replaces a use case
  or a port with a stub, so a test never has to reach inside the object graph.
- **Resources connect lazily.** The engine and cache client are constructed at startup but
  open no connection, so a briefly unavailable dependency delays readiness rather than
  preventing the process from starting. That is what allows an orchestrator to start
  dependencies in any order.

## Consequences

### Positive

- No dependency added; the mechanism is the framework's own, and every FastAPI reader
  already knows it.
- Full static checking: providers are ordinary typed functions.
- Overriding in tests is a one-line, framework-supported operation.
- The composition root is a single file, so "where is this wired" has one answer.

### Negative

- `Depends` is FastAPI's, so the wiring is framework-coupled. Contained: the coupling lives
  in `interfaces`, which is the layer whose job is to be coupled to a delivery mechanism.
  Use cases and adapters are plain classes, constructible without FastAPI, which is what the
  unit tests do.
- Scope management is implicit. FastAPI offers request scope through `yield` dependencies
  and nothing else; process scope is the lifespan, by convention rather than declaration.
- A large graph would make the providers repetitive. Acceptable at this size; the trigger
  for reconsidering is stated below.

### Neutral

- A non-HTTP entry point — a worker, a CLI — cannot reuse these providers. It would call the
  same builders directly, which is a small duplication and a signal to extract a
  framework-neutral container if it happens more than once.

## Alternatives in detail

### Option 2 — dependency-injector

Declarative containers, explicit scopes, and wiring that scales past what `Depends`
comfortably expresses. The right choice once the graph has many nodes with differing
lifetimes, or containers that vary per tenant. Rejected today because it adds a library with
its own DSL and runtime wiring magic, in exchange for solving a problem the project does not
yet have — the kind of anticipatory complexity the project brief explicitly rules out.
Revisit if provider code starts to dominate the composition root.

### Option 3 — Hand-rolled root read from app.state

Total control, no framework coupling in the wiring itself. Rejected because it reimplements
request-scoped teardown, loses FastAPI's `yield`-based cleanup guarantees, and gives up
`dependency_overrides`, which would push every test towards monkeypatching.
