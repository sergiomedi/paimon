# ADR-0006: Continuous integration from Phase 1

- **Status:** Accepted
- **Date:** 2026-09-01
- **Phase:** 1 — Foundation

## Context and problem statement

The project plan places CI/CD in Phase 8, after seven phases of implementation. This ADR
departs from that sequencing and records the reasoning.

The architecture of [ADR-0002](0002-monorepo-layout-and-module-boundaries.md) depends on an
enforcement mechanism: `import-linter` verifying the dependency rule. The type strictness of
[ADR-0005](0005-python-toolchain.md) depends on the same. An enforcement mechanism that runs
only on the author's machine, only when remembered, is not an enforcement mechanism.

Deferring CI to the end has a second, larger cost. Seven phases of code written without a
green build produces a codebase whose failures are all discovered simultaneously, at the
point where they are least separable and most expensive to attribute.

## Decision drivers

- The layer contracts and type contracts must be verified on every change.
- Failures must be attributable to a single commit while that commit is still fresh.
- A test suite that is not run on a clean machine is not known to pass.
- Feedback must stay fast enough to remain part of the loop rather than an obstacle.

## Decision

Split what the project brief treats as one phase into two concerns:

**Phase 1 — continuous integration.** A GitHub Actions workflow on every push and pull
request:

| Job | Contents |
|---|---|
| `quality` | `ruff check`, `ruff format --check`, `mypy --strict`, `import-linter` |
| `test` | `pytest` with coverage, against ephemeral PostgreSQL and Redis service containers |
| `frontend` | `pnpm lint`, `tsc --noEmit`, `pnpm test` |

Scoped with path filters so a documentation change does not run the backend suite, and with
dependency caching keyed on the lockfiles.

**Phase 8 — continuous delivery.** What the brief's Phase 8 is genuinely about: container
image build and publication, environment promotion, migration execution as a deployment
step, deployment to Azure Container Apps, release gating and rollback.

## Consequences

### Positive

- The architecture is enforced from the first commit that could violate it.
- Every commit on `main` is known to build, type-check and pass its tests on a clean
  machine — which is the only place that claim means anything.
- The commit history becomes evidence of engineering discipline, which for a portfolio
  repository is itself a deliverable.
- Phase 8 becomes a focused piece of work on deployment rather than a retrofit of basic
  hygiene across seven phases of accumulated code.

### Negative

- Setup cost in Phase 1, before there is application code to justify it. Roughly a day.
- CI minutes are consumed from the start. Negligible on public repository allowances.
- Risk of a slow pipeline discouraging small commits. Mitigated by path filters, caching,
  and a hard budget: the pull request pipeline stays under five minutes, and exceeding it is
  treated as a defect.

### Neutral

- Branch protection is enabled once the workflow is green, so the rules apply to the author
  as much as to any future contributor.

## Alternatives in detail

### Follow the brief and defer all CI to Phase 8

The argument for it is real: infrastructure written before the code it serves tends to be
guesswork, and there is nothing to test on day one. It is rejected because the specific
things CI must enforce here — layer boundaries and type contracts — decay from the very
first commits, and because "we will add the tests later" has a well-documented success rate.

### Local pre-commit hooks only

Cheaper, and pre-commit *is* configured as the fast local subset. Insufficient on its own:
hooks are bypassable with `--no-verify`, they run on one machine with one environment, and
they cannot host the PostgreSQL and Redis containers the integration suite needs.
