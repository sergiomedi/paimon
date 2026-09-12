#!/usr/bin/env bash
# The hybrid benchmark: real models and real search, against a local database.
#
#   ./scripts/azure/benchmark.sh
#
# Runs the answer benchmark with **Azure OpenAI and Azure AI Search** doing the
# work and PostgreSQL and Redis local in Docker, and writes the report to
# evaluation/reports/azure-<date>.json.
#
# This is the one thing in this phase that sends a real query through the Azure
# AI Search adapter. The deployed application does not: its configuration leaves
# retrieval.store at the default, so the measured run retrieves from pgvector and
# the search service bills all afternoon without being asked a single question.
# Until this has run, "the Azure adapters work" is a claim about a stand-in that
# the author of the adapter also wrote — which is exactly what hid a defect in the
# Entra adapter from Phase 1 until the first deployed request.
#
# It needs no container, no private network and no deployed application. It needs
# a laptop, `az login`, and this environment's outputs. It costs the search
# service's hourly rate — which is already running — plus a few cents of tokens.
#
# Nothing here is an API key, and there is nowhere for one to go: both services
# were created with local authentication disabled (ADR-0037). The identity is
# whoever is signed in, and the deployment granted that person the data-plane
# roles this needs.

source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

require_az
announce
load_outputs

BACKEND="$ROOT/backend"
STAMP="$(date -u +%Y-%m-%dT%H%M)"
REPORT="$ROOT/evaluation/reports/azure-${STAMP}.json"

command -v docker >/dev/null 2>&1 || die "docker is not installed, and the database is local."
command -v uv >/dev/null 2>&1 || die "uv is not installed: https://docs.astral.sh/uv/"

for name in AZURE_OPENAI_ENDPOINT AZURE_SEARCH_ENDPOINT \
            AZURE_CHAT_DEPLOYMENT_NAME AZURE_EMBEDDING_DEPLOYMENT_NAME; do
    [[ -n "${!name:-}" ]] || die "$name is not in the deployment outputs. Deploy first."
done

# The application's own namespace, deliberately: these are settings for the
# process about to run, not variables belonging to the deployment scripts.
export PAIMON_ENVIRONMENT=local
export PAIMON_EMBEDDING__PROVIDER=azure
export PAIMON_CHAT__PROVIDER=azure
export PAIMON_RETRIEVAL__STORE=azure_search
export PAIMON_AZURE_OPENAI__ENDPOINT="$AZURE_OPENAI_ENDPOINT"
export PAIMON_AZURE_OPENAI__CHAT_DEPLOYMENT="$AZURE_CHAT_DEPLOYMENT_NAME"
export PAIMON_AZURE_OPENAI__EMBEDDING_DEPLOYMENT="$AZURE_EMBEDDING_DEPLOYMENT_NAME"
export PAIMON_AZURE_SEARCH__ENDPOINT="$AZURE_SEARCH_ENDPOINT"

bold "▸ what this will use"
printf '  models     %s\n' "$AZURE_OPENAI_ENDPOINT"
printf '  search     %s\n' "$AZURE_SEARCH_ENDPOINT"
printf '  database   local, in docker\n'
printf '  report     %s\n\n' "${REPORT#"$ROOT"/}"

bold "▸ postgres and redis"
docker compose -f "$ROOT/docker/compose.yaml" up -d postgres redis
# Compose returns when the containers are started, which is before PostgreSQL is
# accepting connections. The migration below is the first thing that needs one.
sleep 5
printf '\n'

cd "$BACKEND"

bold "▸ dependencies"
# --extra azure: the Entra credential is an optional extra, and without it the
# adapters fall back to nothing rather than to a key — there is no key.
uv sync --extra azure --quiet
printf '  installed\n\n'

bold "▸ local schema"
uv run --no-sync alembic upgrade head
printf '\n'

bold "▸ the index"
# The definition is the application's, and creating it is a deliberate act by
# somebody with the privilege — not the workload, which must not be able to
# redefine the index it writes to.
if ! uv run --no-sync python -m paimon.interfaces.cli.ensure_search_index; then
    printf '\n'
    warn "The index could not be created. The usual cause is a role assignment that has"
    warn "not propagated: the deployment grants you Search Index Data Contributor, and"
    warn "Entra takes minutes to publish it. Wait and run this again."
    warn ""
    warn "  az role assignment list --assignee \"\$(az ad signed-in-user show --query id -o tsv)\" \\"
    warn "    --scope \"\$(az search service show --name ${AZURE_SEARCH_NAME:-} \\"
    warn "      --resource-group $GROUP --query id -o tsv)\" -o table"
    die "nothing measured."
fi
printf '\n'

bold "▸ benchmarking"
printf 'Fifteen questions, each retrieved from Azure AI Search and answered by Azure\n'
printf 'OpenAI, with every citation verified against the corpus. A few minutes.\n\n'
uv run --no-sync python -m paimon.interfaces.cli.evaluate --answers \
    --corpus "$ROOT/evaluation/corpus/sample" \
    --dataset "$ROOT/evaluation/datasets/retrieval-v1.jsonl" \
    --label "azure openai + ai search" \
    --report "$REPORT"

printf '\n'
bold "▸ written to ${REPORT#"$ROOT"/}"
printf 'Commit it beside the measurement it belongs to. This is the number worth quoting:\n'
printf 'it was measured against the services a deployment would actually use.\n\n'
printf 'To compare it with the local backend, question by question rather than aggregate\n'
printf 'against aggregate:\n'
printf '  cd backend && uv run python -m paimon.interfaces.cli.evaluate --answers \\\n'
printf '    --corpus ../evaluation/corpus/sample \\\n'
printf '    --dataset ../evaluation/datasets/retrieval-v1.jsonl \\\n'
printf '    --against ../%s\n' "${REPORT#"$ROOT"/}"
