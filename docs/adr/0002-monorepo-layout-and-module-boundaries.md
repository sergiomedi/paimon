# ADR-0002: Monorepo layout and enforced module boundaries

- **Status:** Accepted
- **Date:** 2026-09-01
- **Phase:** 1 — Foundation

## Context and problem statement

The platform spans two ecosystems (Python and TypeScript) and several concerns that grow
into large subsystems: retrieval, agent orchestration, evaluation, infrastructure as code.
Two questions must be answered before any code exists.

First, **repository topology**: one repository or several. Second, and more consequential,
**where RAG and agent code lives** relative to the backend, and how the Clean Architecture
dependency rule is prevented from decaying into a diagram nobody obeys.

The original project brief proposed `agents/`, `rag/` and `tests/` as top-level
directories, siblings of `backend/`. This ADR departs from that proposal and records why.

## Decision drivers

- Business logic must stay independent of frameworks (a hard project constraint).
- The dependency rule must be verifiable, not merely documented.
- Import paths and test discovery must be obvious to a newcomer.
- The structure must accommodate extracting a service later without a rewrite.

## Considered options

1. **Single repository; RAG and agents as modules inside the backend package**, boundaries
   enforced by a static import checker.
2. **Single repository; RAG and agents as top-level directories** (the original brief).
3. **Single repository as a `uv` workspace** with `paimon-rag` and `paimon-agents` as
   separately versioned packages.
4. **Multiple repositories**, one per subsystem.

## Decision

Option 1. A single repository laid out as:

```text
backend/src/paimon/{domain,application,rag,agents,infrastructure,interfaces}
backend/tests/{unit,integration,e2e}
frontend/ evaluation/ infrastructure/ docker/ docs/ .github/workflows/
```

with the dependency rule enforced in CI by `import-linter`, configured as a layered
contract:

```text
interfaces -> application -> domain
infrastructure -> domain
```

`interfaces/api/dependencies.py` is the composition root and the only module permitted to
import from `infrastructure`.

**Why not the top-level layout of the brief.** RAG and agents are not independent systems.
They import domain entities, they are invoked by application use cases, and they deploy in
the same process as the API. Placing them outside `backend/` produces one of two outcomes:
either they become installable packages with their own dependency metadata — the cost of
Option 3 without its benefits — or the project resorts to path manipulation, which breaks
editor tooling, type checking and packaging. A top-level `tests/` directory adds a third
problem: two test ecosystems collide in one namespace, and pytest's rootdir and import-mode
resolution become ambiguous.

**Why enforcement matters more than layout.** Any of these layouts can express Clean
Architecture. None of them *preserves* it. Layer violations enter through ordinary,
well-intentioned commits — a use case importing a SQLAlchemy session to save one
indirection. `import-linter` turns the architecture into a failing build, which is the only
form of documentation that cannot be ignored.

## Consequences

### Positive

- One dependency graph, one lockfile, one type-check pass over the whole backend.
- Atomic commits across layers; a refactor that spans domain and infrastructure is one
  reviewable change.
- The dependency rule is a test. Violations are caught in seconds, by a machine.

### Negative

- A single repository grows large, and CI must be scoped by path filters to stay fast.
  Accepted; addressed in [ADR-0006](0006-continuous-integration-from-phase-1.md).
- Module boundaries are weaker than package boundaries — nothing but the linter stops a
  developer from importing across them. Accepted: the linter is the enforcement.
- Departs from the original project brief. Recorded here deliberately.

### Neutral

- Extracting the agent runtime into its own service later means moving a directory and
  adding a transport, because it already communicates through ports rather than imports.

## Alternatives in detail

### Option 3 — `uv` workspace with separate packages

The correct choice once two subsystems have genuinely divergent dependency sets or
independent release cadences — for example, if the agent runtime needed a CUDA-bound
dependency the API must not carry. Today it buys hard boundaries at the cost of cross-package
refactoring friction, version pinning between internal packages, and a slower editor
experience. Revisit when the agent runtime is extracted into its own deployment.

### Option 4 — Multiple repositories

Appropriate when separate teams own separate release cycles. With one author it converts
every cross-cutting change into a coordinated multi-repository dance, and makes the
portfolio unreadable as a single artifact. Rejected without reservation at this scale.
