# ADR-0005: Python toolchain — uv, ruff, mypy

- **Status:** Accepted
- **Date:** 2026-09-01
- **Phase:** 1 — Foundation

## Context and problem statement

The backend targets Python 3.13 and will accumulate a heavy dependency set: FastAPI,
SQLAlchemy, LangGraph, Azure SDKs, OpenTelemetry, and their transitive closure. Three
decisions must be made before the first dependency is added — package management, linting
and formatting, and type checking — because changing any of them later means touching every
file and every pipeline.

The project's coding standards mandate strong typing, type hints and docstrings throughout.
Standards that are not machine-verified are aspirations; the toolchain has to enforce them.

## Decision drivers

- Reproducible installs, locked transitively, identical locally and in CI.
- Fast enough that the pre-commit hook is not something to disable in a hurry.
- Currently prevalent in AI and LLM engineering roles in Europe and the United States.
- Type checking strict enough to make the architecture's protocols meaningful.

## Considered options

**Package management:** `uv` · Poetry · pip-tools · plain pip with `requirements.txt`.

**Lint and format:** `ruff` · the `black` + `isort` + `flake8` combination.

**Type checking:** `mypy --strict` · `pyright`.

## Decision

**`uv`** for Python version management, virtual environments, dependency resolution and
locking. It replaces `pyenv`, `virtualenv`, `pip` and `pip-tools` with one tool, produces a
cross-platform `uv.lock`, and resolves and installs an environment of this size in seconds
rather than minutes. Its adoption across AI tooling has been rapid, and matching what
teams actually use is an explicit project criterion.

**`ruff`** for both linting and formatting, replacing black, isort and flake8. One tool,
one configuration block, and a full-repository pass in well under a second — which is what
makes running it on every commit sustainable.

**`mypy --strict`** for type checking. Strict from the first commit, because the alternative
is a permanent backlog of untyped modules that never gets paid down. `disallow_untyped_defs`
and `warn_return_any` are what turn the `Protocol` ports of
[ADR-0003](0003-ports-and-adapters-for-llm-and-vector-store.md) into a real contract rather
than a naming convention.

Supporting tools: **`pytest`** with `pytest-asyncio` and `pytest-cov`; **`import-linter`**
for the layer contracts of [ADR-0002](0002-monorepo-layout-and-module-boundaries.md);
**`pre-commit`** to run the fast subset locally.

The frontend uses **`pnpm`** for the same reasons: a strict, content-addressed store, fast
installs and a reliable lockfile.

## Consequences

### Positive

- Environment setup for a new contributor is `uv sync` — one command, no Python version
  negotiation.
- CI installs in seconds, which keeps the feedback loop tight enough to be useful.
- Strict typing catches port-contract violations at author time instead of at runtime.
- One formatter, so formatting stops being a review topic.

### Negative

- `uv` is younger than Poetry and its interface has moved faster. Mitigated by the lockfile
  being the source of truth and by pinning the `uv` version in CI.
- `mypy --strict` against SQLAlchemy and some Azure SDKs requires targeted stub work and
  occasional narrowly scoped ignores. Accepted: every ignore is scoped to a rule and carries
  a comment explaining it.
- Strictness slows early velocity. That is the trade being made, deliberately.

### Neutral

- `ruff` does not yet cover every `flake8` plugin in existence. Nothing currently required
  is missing.

## Alternatives in detail

### Poetry

Mature, widely deployed, and a reasonable choice. Rejected on resolution speed with large
scientific dependency trees, on its historically loose adherence to packaging standards, and
because `uv` additionally manages Python versions, removing a separate tool from the setup
instructions.

### pyright over mypy

Faster, with better inference in several cases, and excellent editor integration. Rejected
because mypy remains the more common choice in Python backend codebases and integrates more
predictably in CI without a Node runtime. Would be reconsidered if inference limitations
became an actual obstacle.
