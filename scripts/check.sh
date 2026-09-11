#!/usr/bin/env bash
# Run the same gates CI runs, in the same order, and stop at the first failure.
#
#   ./scripts/check.sh                  backend, frontend and infrastructure
#   ./scripts/check.sh backend          backend only
#   ./scripts/check.sh infrastructure   the Azure templates only
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
    # --all-extras too: the Azure credential path is an optional extra, and
    # mypy cannot verify code whose imports are not installed.
    step "uv sync";        uv sync --all-groups --all-extras
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

if [[ "$TARGET" == "all" || "$TARGET" == "infrastructure" ]]; then
    cd "$ROOT"

    # The deployment scripts, and the programs inside them.
    #
    # `bash -n` alone is what let a broken deploy.sh ship: it checks the shell
    # and says nothing about a Python program passed to `python3 -c` as a string,
    # because to the shell that string is an argument. So the Python lives in a
    # file now, and this compiles it and then *runs* it against a fixture —
    # compiling would have caught that bug, and running is what catches the next
    # one.
    step "scripts"
    find scripts -name '*.sh' -print0 | xargs -0 -n1 bash -n
    python3 -m py_compile scripts/azure/outputs.py
    printf '{"aB": {"value": "x"}}' | python3 scripts/azure/outputs.py | grep -qx 'AZURE_A_B=x'

    # The collector's configuration, parsed rather than trusted. Bicep loads it
    # with loadTextContent(), which checks that the file exists and nothing else.
    # Compiled first and then *run*, for the reason in the comment above this one.
    # The quota checker, compiled and then run against a fixture built from real
    # Azure output — including the row whose name disagrees with the model's
    # (`gpt4.1-mini` against `gpt-4.1-mini`) and the dot inside it, which a naive
    # split drops. Both of those cost a day each; neither can cost another.
    step "model quota"
    python3 -m py_compile scripts/azure/model_quota.py
    python3 - <<'PYTHON'
import json
import pathlib
import sys

sys.path.insert(0, "scripts/azure")
from model_quota import Wanted, missing, normalise  # noqa: E402

assert normalise("gpt-4.1-mini") == normalise("gpt4.1-mini"), "the two spellings must agree"

usage = [
    {"name": {"value": "OpenAI.GlobalStandard.gpt4.1-mini"}, "limit": 200},
    {"name": {"value": "OpenAI.Standard.text-embedding-3-large"}, "limit": 350},
    {"name": {"value": "OpenAI.GlobalStandard.text-embedding-3-large"}, "limit": 0},
]
assert not missing(usage, [Wanted("GlobalStandard", "gpt-4.1-mini")]), "dotted name must match"
assert missing(usage, [Wanted("GlobalStandard", "text-embedding-3-large")]), "limit 0 is not quota"
assert missing(usage, [Wanted("Standard", "gpt-4.1-mini")]), "the deployment type matters"
assert not missing(usage, [Wanted("Standard", "text-embedding-3-large")])
print("  quota matching holds across both spellings")
PYTHON

    step "collector config"
    python3 -m py_compile scripts/azure/collector_config.py
    python3 scripts/azure/collector_config.py

    # Either the standalone CLI or the one the Azure CLI manages. Compiling is
    # the gate: the linter runs inside it, and infrastructure/bicepconfig.json
    # raises the rules that matter here to errors, so a template that builds is a
    # template that passed them.
    if command -v bicep >/dev/null 2>&1; then
        BICEP=(bicep)
    elif command -v az >/dev/null 2>&1 && az bicep version >/dev/null 2>&1; then
        BICEP=(az bicep)
    else
        BICEP=()
    fi

    if [[ ${#BICEP[@]} -gt 0 ]]; then
        cd "$ROOT/infrastructure"
        step "bicep build"
        if [[ "${BICEP[0]}" == "az" ]]; then
            az bicep build --file main.bicep --stdout >/dev/null
            az bicep build-params --file main.bicepparam --stdout >/dev/null
        else
            bicep build main.bicep --stdout >/dev/null
            bicep build-params main.bicepparam --stdout >/dev/null
        fi
    else
        printf '\n\033[33mSkipping infrastructure: install Bicep (az bicep install) to check the templates.\033[0m\n'
    fi
fi

printf '\n\033[32mAll checks passed.\033[0m\n'
