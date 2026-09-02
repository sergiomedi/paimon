#!/usr/bin/env bash
# Run the same gates CI runs, in the same order, and stop at the first failure.
#
#   ./scripts/check.sh              backend and frontend
#   ./scripts/check.sh backend      backend only
#
# This exists because a change once reached CI having passed its tests but not
# its linter: the checks were run individually and one was forgotten. One
# command removes that possibility. Keep it in step with .github/workflows/ci.yml
# — CI remains the authority, this is the local mirror of it.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="${1:-all}"

step() { printf '\n\033[1m▸ %s\033[0m\n' "$1"; }

if [[ "$TARGET" == "all" || "$TARGET" == "backend" ]]; then
    cd "$ROOT/backend"
    # --all-groups, because a bare `uv run` installs only the default groups and
    # would leave pytest out — which mypy then reports as thirty missing-import
    # errors rather than as the missing dependency it is. CI syncs the same way.
    step "uv sync";        uv sync --all-groups
    # --no-sync from here on: the environment is already correct, and re-checking
    # it before every gate only costs time.
    step "ruff check";     uv run --no-sync ruff check .
    step "ruff format";    uv run --no-sync ruff format --check .
    step "mypy --strict";  uv run --no-sync mypy
    step "import-linter";  uv run --no-sync lint-imports
    step "pytest";         uv run --no-sync pytest
fi

if [[ "$TARGET" == "all" || "$TARGET" == "frontend" ]]; then
    if [[ -d "$ROOT/frontend/node_modules" ]]; then
        cd "$ROOT/frontend"
        step "eslint";     pnpm lint
        step "tsc";        pnpm typecheck
        step "next build"; pnpm build
    else
        printf '\n\033[33mSkipping frontend: run pnpm install in frontend/ first.\033[0m\n'
    fi
fi

printf '\n\033[32mAll checks passed.\033[0m\n'
