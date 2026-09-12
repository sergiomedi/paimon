#!/usr/bin/env bash
# The measured run: what this deployment actually does, written down.
#
#   ./scripts/azure/measure.sh
#
# Times a cold start and a warm request, ingests a document and asks a question
# about it through the deployed API, checks that a trace reached Application
# Insights, and records every number in docs/measurements/<env>-<date>.md.
#
# The recording is the point. This environment exists to be measured once and
# destroyed, so every figure here becomes unavailable the moment the resource
# group does — and a measurement that lives only in a terminal that has since
# been closed was not a measurement. The file it writes is the deliverable; the
# terminal output is a copy of it.
#
# It creates nothing in Azure and costs a few cents of tokens.

source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

require_az
announce
load_outputs

[[ -n "${AZURE_API_URL:-}" ]] || die "$(printf '%s\n' \
    "this environment has no application deployed — apiUrl is empty." \
    "" \
    "  ./scripts/azure/publish.sh && ./scripts/azure/deploy.sh")"

command -v curl >/dev/null 2>&1 || die "curl is not installed, and the measurements are HTTP."

API="${AZURE_API_URL%/}"
REPORT="$ROOT/docs/measurements/${ENVIRONMENT}-$(date -u +%Y-%m-%dT%H%M).md"
mkdir -p "$(dirname "$REPORT")"

BODY="$(mktemp)"
trap 'rm -f "$BODY" "${BODY}.request"' EXIT

# Everything this prints goes to the terminal and to the report, because the two
# audiences are the same person twenty minutes apart.
record() { printf '%s\n' "$*" | tee -a "$REPORT"; }

# One timed request. Leaves the response body in $BODY and sets STATUS and
# SECONDS_TAKEN.
#
# --max-time rather than an open wait: Container Apps closes an ingress
# connection at 240 seconds, so anything past that is the platform's answer
# rather than the application's, and a hung curl measures neither.
request() {
    local method="$1" path="$2" data="${3:-}" measured
    local -a arguments=(
        --silent --show-error --max-time 250
        --request "$method" "${API}${path}"
        --header "Authorization: Bearer ${TOKEN}"
        --output "$BODY"
        --write-out '%{http_code} %{time_total}'
    )
    [[ -n "$data" ]] && arguments+=(--header 'Content-Type: application/json' --data "@${data}")

    # `|| true`: a curl that fails still has a measurement in it — a timeout is a
    # result — and `set -e` would end the run instead of recording it.
    measured="$(curl "${arguments[@]}" 2>>"$BODY" || printf '000 0')"
    STATUS="${measured%% *}"
    SECONDS_TAKEN="${measured##* }"
}

# What a status code means here, said once rather than guessed at each call site.
verdict() {
    case "$1" in
        2*) printf 'ok' ;;
        401) printf 'rejected — the token or the audience (./scripts/azure/token.sh)' ;;
        403) printf 'forbidden — usually a role assignment that has not propagated' ;;
        000) printf 'no response within 250 seconds' ;;
        5*) printf 'the application answered with an error; the body is below' ;;
        *) printf 'unexpected' ;;
    esac
}

# ─────────────────────────────────────────────────────────────────────────────

record "# Measured run — ${ENVIRONMENT}"
record ""
record "| | |"
record "|---|---|"
record "| when | $(date -u '+%Y-%m-%d %H:%M UTC') |"
record "| region | ${LOCATION} |"
record "| api | ${API} |"
record "| image | ${AZURE_API_IMAGE_DEPLOYED:-unknown} |"
record "| database | ${AZURE_DATABASE_HOST:-unknown} |"
record ""

bold "▸ a token to call it with"
APP_ID="$(api_app_id)"
ensure_app_registration "$APP_ID"
TOKEN="$(api_token "$APP_ID")" || die "no token, so there is nothing to measure."
printf '  acquired (not printed, and not written to the report)\n\n'

# ── Cold start ───────────────────────────────────────────────────────────────
#
# The application scales to zero, so the first request after an idle period pays
# for an image pull, a process start and a connection pool. That number is the
# one nobody publishes and everybody meets, and it is only available once: the
# second request cannot be a cold start however it is labelled.
bold "▸ replicas before the first request"
REPLICAS="$(az containerapp replica list --name "${AZURE_API_NAME:-ca-paimon-api-${ENVIRONMENT}}" \
    --resource-group "$GROUP" --query 'length(@)' -o tsv 2>/dev/null || printf '')"
REPLICAS="${REPLICAS:-0}"
printf '  %s\n\n' "$REPLICAS"

bold "▸ first request"
request GET /api/v1/health/live
if [[ "$REPLICAS" -eq 0 ]]; then
    record "**Cold start: ${SECONDS_TAKEN}s** (HTTP ${STATUS}, $(verdict "$STATUS"))."
    record ""
    record "No replica was running: this includes the image pull, the process start and the"
    record "first connection pool. It is measurable exactly once per idle period."
else
    record "**First request: ${SECONDS_TAKEN}s** (HTTP ${STATUS}, $(verdict "$STATUS"))."
    record ""
    record "${REPLICAS} replica(s) were already running, so this is *not* a cold start. To"
    record "measure one, leave the environment idle for the scale-to-zero cooldown — about"
    record "five minutes — and run this again."
fi
record ""
printf '  %ss  HTTP %s\n\n' "$SECONDS_TAKEN" "$STATUS"

# ── Warm ─────────────────────────────────────────────────────────────────────
bold "▸ warm requests"
WARM=()
for _ in 1 2 3 4 5; do
    request GET /api/v1/health/ready
    WARM+=("$SECONDS_TAKEN")
    printf '  %ss  HTTP %s\n' "$SECONDS_TAKEN" "$STATUS"
done
record "**Warm readiness**: ${WARM[*]} seconds."
record ""
record "Readiness rather than liveness on purpose: it opens the database, the cache and the"
record "model endpoint, so it is the cheapest request that touches everything."
record ""
printf '\n'

# ── Authentication ───────────────────────────────────────────────────────────
#
# A deployment that answers is not the same as a deployment that refuses. Both
# are worth one line, and the second is the one nobody checks.
bold "▸ without a token"
ANONYMOUS="$(curl --silent --show-error --max-time 30 --output /dev/null \
    --write-out '%{http_code}' "${API}/api/v1/me" || printf '000')"
record "**Unauthenticated call**: HTTP ${ANONYMOUS} (401 is the expected answer)."
record ""
printf '  HTTP %s\n\n' "$ANONYMOUS"

bold "▸ with one"
request GET /api/v1/me
record "**Authenticated call**: HTTP ${STATUS} in ${SECONDS_TAKEN}s — $(verdict "$STATUS")."
record ""
printf '  %ss  HTTP %s\n\n' "$SECONDS_TAKEN" "$STATUS"
if [[ "$STATUS" != 2* ]]; then
    record '```'
    record "$(head -c 600 "$BODY")"
    record '```'
    record ""
    warn "Authentication failed, so ingestion and answering would measure nothing."
    warn "  ./scripts/azure/token.sh   — which of the four causes it is"
    die "stopped here. The report so far is at $REPORT"
fi

# ── End to end ───────────────────────────────────────────────────────────────
DOCUMENT="oncall-handbook"
QUESTION="How long do I have to acknowledge a page, and what happens if I do not?"

bold "▸ ingesting ${DOCUMENT}"
python3 "$SCRIPTS/measurement.py" body \
    --document-id "$DOCUMENT" \
    --source-uri "file://${DOCUMENT}.md" \
    "$ROOT/evaluation/corpus/sample/${DOCUMENT}.md" > "${BODY}.request"
request PUT "/api/v1/documents/${DOCUMENT}" "${BODY}.request"
record "**Ingestion**: HTTP ${STATUS} in ${SECONDS_TAKEN}s — $(head -c 300 "$BODY")"
record ""
record "Chunked, embedded through Azure OpenAI and indexed into PostgreSQL over a private"
record "endpoint, as one request."
record ""
printf '  %ss  HTTP %s\n\n' "$SECONDS_TAKEN" "$STATUS"

bold "▸ asking about it"
printf '{"question": "%s"}' "$QUESTION" > "${BODY}.request"
request POST /api/v1/answers "${BODY}.request"
FIRST_ANSWER="$SECONDS_TAKEN"
record "**First answer**: HTTP ${STATUS} in ${SECONDS_TAKEN}s."
record ""
record "> *${QUESTION}*"
record ""
record "$(python3 "$SCRIPTS/measurement.py" answer < "$BODY" || true)"
record ""
printf '  %ss  HTTP %s\n\n' "$SECONDS_TAKEN" "$STATUS"

bold "▸ asking again"
request POST /api/v1/answers "${BODY}.request"
record "**Same question again**: ${SECONDS_TAKEN}s, against ${FIRST_ANSWER}s."
record ""
record "The gap is the semantic cache and whatever the model's own caching contributes; the"
record "cache is per replica (ADR-0039), so at one replica this is its best case rather than"
record "its average."
record ""
printf '  %ss  HTTP %s\n\n' "$SECONDS_TAKEN" "$STATUS"

# ── Telemetry ────────────────────────────────────────────────────────────────
#
# Asked of the workspace rather than of the collector. The collector's health
# endpoint reports that it is accepting data, not that it is delivering it: an
# exporter that cannot authenticate looks healthy and drops everything.
bold "▸ did the traces arrive"
WORKSPACE="$(az monitor log-analytics workspace show --resource-group "$GROUP" \
    --workspace-name "log-paimon-${ENVIRONMENT}" --query customerId -o tsv 2>/dev/null || printf '')"
TRACES="unknown"
if [[ -n "$WORKSPACE" ]]; then
    # A minute or two of ingestion lag is normal, and these requests were seconds
    # ago, so an empty answer here is not yet evidence of anything.
    sleep 60
    TRACES="$(az monitor log-analytics query --workspace "$WORKSPACE" --analytics-query \
        "AppRequests | where TimeGenerated > ago(30m) | summarize n = count() | project n" \
        --query '[0].n' -o tsv 2>/dev/null || printf 'unknown')"
fi
record "**Requests in Application Insights in the last 30 minutes**: ${TRACES}."
record ""
if [[ "$TRACES" == "unknown" || "$TRACES" == "0" ]]; then
    record "Nothing arrived, or the query could not run. The collector is the first place to"
    record "look, not the second — an exporter that cannot authenticate is healthy and silent:"
    record ""
    record '```'
    record "az containerapp logs show --name ca-paimon-otel-${ENVIRONMENT} --resource-group ${GROUP} --tail 100"
    record '```'
    record ""
fi
printf '  %s\n\n' "$TRACES"

# ─────────────────────────────────────────────────────────────────────────────

record "## What is still to be filled in by hand"
record ""
record "- **Actual spend**, from Cost Management in the portal, per service. Not the"
record "  estimate — the invoice is the only authority on what this cost."
record "- **The hybrid benchmark** against the real models and search service, per"
record "  \`docs/deployment.md\`. That is the number worth quoting: it was measured against"
record "  what a deployment would actually use."
record "- **One span tree**, from Application Insights' transaction view, to show what a"
record "  single request looks like end to end."
record ""

printf '\n'
bold "▸ written to ${REPORT#"$ROOT"/}"
printf 'Commit it. Every number in it stops being available the moment the resource group\n'
printf 'does, and outliving the environment is the whole argument for having one.\n\n'
printf 'Then:\n'
printf '  ./scripts/azure/status.sh     what still exists and what it bills\n'
printf '  ./scripts/azure/destroy.sh    remove it\n'
printf '  ./scripts/azure/status.sh     prove the table is empty\n'
