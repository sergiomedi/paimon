#!/usr/bin/env bash
# The agent benchmark against real models: does a more capable model take the
# second hop?
#
#   ./scripts/azure/agents.sh
#   ./scripts/azure/agents.sh --ceiling 9      stop if the run would cost more
#
# Runs all four systems — answers, incident-triage, investigator-v1 and
# investigator — over agents-v1, k=3, with **Azure OpenAI and Azure AI Search**
# doing the work and PostgreSQL local in Docker. Writes one report per system to
# evaluation/reports/azure-agents-<system>-<date>.json.
#
# WHAT THIS IS FOR. Locally, qwen2.5:7b-instruct searched once and stopped in
# 100 of 100 held-out runs, on both passage layouts — proved to be the model's
# choice rather than a cut loop: no limit fired, every run reached its second
# `act`, and a proxy in front of Ollama showed that request carrying both tool
# definitions. Whether a stronger model behaves differently is the open
# question, and it cannot be answered by a stronger prompt on the same model.
#
# So the primary diagnostic is **not** the pass rate. It is:
#
#   · tool calls per run, as a distribution rather than a mean, and
#   · how many attempts cite two distinct documents.
#
# The local baseline both layouts have to beat is {1: 50} calls, and 11 and 10
# attempts out of 50 reaching two documents.
#
# It needs no container, no private network and no deployed application — the
# same footing as benchmark.sh. Local authentication is disabled on both
# services (ADR-0037), so there is no key: the identity is whoever is signed in.
#
# THE CEILING IS REAL, AND IT COUNTS TOKENS ONLY. Each report carries what its
# tokens cost at the price table below, and this stops before starting a system
# it cannot afford. If a report comes back without a cost it stops too, because
# a ceiling that cannot read the bill is decoration.
#
# What it does **not** count is the environment's hourly charge. The deployment
# bills roughly €0.25 an hour for the database whether or not anything queries
# it, and at these token volumes that dominates: the models cost cents and the
# clock costs euros. So the ceiling bounds the model bill, the wall clock bounds
# the rest, and the only control on the second is destroying the environment
# when the run finishes. Both figures are reported at the end; neither is
# presented as the whole bill.

source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

CEILING=9
if [[ "${1:-}" == "--ceiling" ]]; then
    CEILING="${2:?--ceiling needs a number of dollars}"
fi

require_az
announce
load_outputs

BACKEND="$ROOT/backend"
STAMP="$(date -u +%Y-%m-%d)"
TRIALS=3

# Every run in the window, in order, as `tag|system|dataset|tenant`.
#
# Two datasets, two tenants, one budget. The v1 tenant gets **exactly** the
# corpus the local v1 run used — nine documents, nothing added — because its
# numbers are only comparable with the local ones if the retrieval landscape is
# the same. The held-out tenant gets those nine plus the three held-out
# documents, which is the corpus the local held-out run used, and it is separate
# so that adding them cannot disturb the v1 measurement.
#
# The held-out pair is here for the primary question rather than for a pass
# rate. agents-v1 has seven multi-hop tasks and the held-out set six; together
# thirteen, which is still small and is nearly twice what either gives alone.
RUNS=(
    "v1|answers|agents-v1.jsonl|azure-agents"
    "v1|incident-triage|agents-v1.jsonl|azure-agents"
    "v1|investigator-v1|agents-v1.jsonl|azure-agents"
    "v1|investigator|agents-v1.jsonl|azure-agents"
    "heldout|investigator|agents-v2-heldout.jsonl|azure-heldout"
    "heldout|investigator-v1|agents-v2-heldout.jsonl|azure-heldout"
)

command -v docker >/dev/null 2>&1 || die "docker is not installed, and the database is local."
command -v uv >/dev/null 2>&1 || die "uv is not installed: https://docs.astral.sh/uv/"
command -v python3 >/dev/null 2>&1 || die "python3 is not installed, and the ceiling is arithmetic."

for name in AZURE_OPENAI_ENDPOINT AZURE_SEARCH_ENDPOINT \
            AZURE_CHAT_DEPLOYMENT_NAME AZURE_EMBEDDING_DEPLOYMENT_NAME; do
    [[ -n "${!name:-}" ]] || die "$name is not in the deployment outputs. Deploy first."
done

# The price table, and the date it was read. Cost is token counts times a table
# somebody typed — the invoice is the authority — so the figure is labelled with
# when it was copied rather than presented as a fact about the bill.
PRICE_REVISION="2026-09-24"
#
# Keyed on the **deployment name** as well as the model, because the Azure
# adapter reports `model_id` as the deployment — `paimon-chat`, not
# `gpt-4.1-mini`. A table keyed only on the model produces no cost for every
# attempt, which the ceiling below reads as "unmetered" and stops on. Correct
# behaviour, and a stop on the first system rather than a measurement.
PRICES="$(python3 - <<PY
import json, os
rate = {"input": 0.40, "output": 1.60}
print(json.dumps({"gpt-4.1-mini": rate, os.environ["AZURE_CHAT_DEPLOYMENT_NAME"]: rate}))
PY
)"

export PAIMON_ENVIRONMENT=local
export PAIMON_EMBEDDING__PROVIDER=azure
export PAIMON_CHAT__PROVIDER=azure
export PAIMON_RETRIEVAL__STORE=azure_search
export PAIMON_AZURE_OPENAI__ENDPOINT="$AZURE_OPENAI_ENDPOINT"
export PAIMON_AZURE_OPENAI__CHAT_DEPLOYMENT="$AZURE_CHAT_DEPLOYMENT_NAME"
export PAIMON_AZURE_OPENAI__EMBEDDING_DEPLOYMENT="$AZURE_EMBEDDING_DEPLOYMENT_NAME"
export PAIMON_AZURE_SEARCH__ENDPOINT="$AZURE_SEARCH_ENDPOINT"
export PAIMON_OBSERVABILITY__METRICS__PRICING__MODELS="$PRICES"

# The judge stays off here. Grading is a second pass with phi4 locally, over
# these transcripts, so the judge never changes between the runs it compares and
# no Azure token is spent on a decision a local model already makes.
export PAIMON_EVALUATION__JUDGE__ENABLED=false

bold "▸ what this will use"
printf '  models     %s  (%s)\n' "$AZURE_OPENAI_ENDPOINT" "$AZURE_CHAT_DEPLOYMENT_NAME"
printf '  search     %s\n' "$AZURE_SEARCH_ENDPOINT"
printf '  database   local, in docker\n'
printf '  runs       %s\n' "${#RUNS[@]}"
for spec in "${RUNS[@]}"; do
    IFS='|' read -r tag system dataset tenant <<<"$spec"
    printf '               %-8s %-16s %-22s %s\n' "$tag" "$system" "$dataset" "$tenant"
done
printf '  trials     %s per task\n' "$TRIALS"
printf '  ceiling    $%s, checked between systems\n' "$CEILING"
printf '  prices     %s  (read %s)\n\n' "$PRICES" "$PRICE_REVISION"

bold "▸ postgres and redis"
docker compose -f "$ROOT/docker/compose.yaml" up -d postgres redis
sleep 5
printf '\n'

cd "$BACKEND"

bold "▸ dependencies"
uv sync --extra azure --quiet
printf '  installed\n\n'

bold "▸ local schema"
uv run --no-sync alembic upgrade head
printf '\n'

bold "▸ the index"
if ! uv run --no-sync python -m paimon.interfaces.cli.ensure_search_index; then
    printf '\n'
    warn "The index could not be created. The usual cause is a role assignment that"
    warn "has not propagated — Entra takes minutes to publish one. Wait and retry."
    die "nothing measured."
fi
printf '\n'

STARTED=$(date +%s)
spent=0

# The most any one system has cost so far, used to decide whether the next one
# can be afforded. A ceiling can only be checked between systems — nothing here
# can stop a benchmark halfway through — so "spent < ceiling" is not enough: at
# $7.50 of $9 it would start a system that costs $2 and finish at $9.50. The
# guarantee this gives instead is: **the run stops before starting anything that
# could carry it past the ceiling, assuming no system costs more than the
# dearest one already measured.** The first system is bounded by the estimate
# below rather than by an observation.
dearest=0

# A conservative prior for the first system, since there is nothing measured yet
# to project from. 30 tasks x 3 trials x 4000 tokens is about double what the
# local run spent per attempt, priced as though every token were an output one.
FIRST_ESTIMATE="$(python3 -c "print(f'{30 * 3 * 4000 / 1e6 * 1.60:.4f}')")"

# What a finished report says it cost. Prints nothing and returns non-zero when
# the report carries no cost, which the caller treats as a stop rather than as a
# zero: a model nobody priced produces silence, and silence is not free.
report_cost() {
    python3 - "$1" <<'PY'
import json, sys
raw = json.loads(open(sys.argv[1], encoding="utf-8").read())
cost = (raw.get("trajectory") or {}).get("estimated_cost")
if cost is None:
    sys.exit(1)
print(f"{cost:.4f}")
PY
}

for spec in "${RUNS[@]}"; do
    IFS='|' read -r tag system dataset tenant <<<"$spec"

    # The v1 corpus alone, or the v1 corpus plus the held-out documents. Never
    # the held-out documents alone: three documents with no distractors mean
    # every search returns nearly the whole corpus, and the failure worth
    # measuring — the right document retrieved at the wrong chunk — cannot occur.
    corpus=("$ROOT/evaluation/corpus/sample")
    [[ "$tag" == "heldout" ]] && corpus+=("$ROOT/evaluation/corpus/heldout")

    report="$ROOT/evaluation/reports/azure-agents-${tag}-${system}-${STAMP}.json"
    journal="$ROOT/evaluation/reports/azure-agents-${tag}-${system}-journal.jsonl"

    # Checked before starting, against what this system could cost rather than
    # against what has been spent. A ceiling enforced after the spending is a
    # receipt.
    projected="$dearest"
    python3 -c "import sys; sys.exit(0 if float('$projected') > 0 else 1)" \
        || projected="$FIRST_ESTIMATE"
    if python3 -c "import sys; sys.exit(0 if float('$spent') + float('$projected') > float('$CEILING') else 1)"; then
        printf '\n'
        warn "STOPPED at the ceiling: \$$spent spent, '$tag/$system' could cost \$$projected,"
        warn "and \$$CEILING is the limit. '$tag/$system' was not started."
        warn "The reports already written are complete and usable."
        break
    fi

    bold "▸ $tag / $system  (x$TRIALS, spent \$$spent of \$$CEILING, this one up to \$$projected)"
    uv run --no-sync python -m paimon.interfaces.cli.evaluate \
        --agents --agent "$system" \
        --dataset "$ROOT/evaluation/datasets/$dataset" \
        --corpus "${corpus[@]}" \
        --tenant "$tenant" --trials "$TRIALS" \
        --label "gpt-4.1-mini via azure openai, azure ai search, ${dataset%.jsonl}" \
        --report "$report" \
        --journal "$journal"

    if ! cost="$(report_cost "$report")"; then
        printf '\n'
        warn "'$tag/$system' finished but its report has no cost, so the ceiling cannot"
        warn "be enforced for the next system. The usual cause is a price table whose"
        warn "model name does not match what the deployment reports."
        die "stopping rather than spending unmetered."
    fi
    spent="$(python3 -c "print(f'{float(\"$spent\") + float(\"$cost\"):.4f}')")"
    dearest="$(python3 -c "print(f'{max(float(\"$dearest\"), float(\"$cost\")):.4f}')")"
    printf '\n  %s/%s cost $%s, running total $%s\n\n' "$tag" "$system" "$cost" "$spent"
done

printf '\n'
ELAPSED=$(( ($(date +%s) - STARTED) / 60 ))
bold "▸ tokens cost \$$spent of a \$$CEILING ceiling, in $ELAPSED minutes"
printf 'The environment also bills about EUR 0.25 an hour while it exists, which the\n'
printf 'ceiling does not count: %s minutes is about EUR %s. Destroy it to stop that.\n\n' \
    "$ELAPSED" "$(python3 -c "print(f'{$ELAPSED / 60 * 0.25:.2f}')")"
printf 'Reports are in evaluation/reports/azure-agents-*-%s.json.\n\n' "$STAMP"
printf 'The primary diagnostic, against the local baseline of {1: 50} calls and\n'
printf '11 and 10 of 50 attempts reaching two documents:\n'
printf '  scripts/azure/hops.py evaluation/reports/azure-agents-*-%s.json\n\n' "$STAMP"
printf 'Grade them with phi4 locally — the judge does not change between runs:\n'
printf '  PAIMON_EVALUATION__JUDGE__ENABLED=true PAIMON_EVALUATION__JUDGE__MODEL=phi4 \\\n'
printf '    uv run python -m paimon.interfaces.cli.evaluate --agents --agent <system> \\\n'
printf '    --dataset ../evaluation/datasets/agents-v1.jsonl \\\n'
printf '    --corpus ../evaluation/corpus/sample --tenant %s \\\n' "$TENANT"
printf '    --regrade <report> --report <graded>\n\n'
printf 'Then destroy the environment and check nothing was left soft-deleted:\n'
printf '  ./scripts/azure/destroy.sh\n\n'
