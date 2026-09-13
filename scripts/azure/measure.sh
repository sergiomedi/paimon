#!/usr/bin/env bash
# The measured run: what this deployment actually does, written down.
#
#   ./scripts/azure/measure.sh
#
# Times a cold start and a warm request, ingests a document and asks a question
# about it through the deployed API, checks that a trace reached Application
# Insights, and records every number in docs/measurements/<env>-<date>.md.
#
#   ./scripts/azure/measure.sh --cold    wait for the app to scale to zero first
#
# A cold start is only measurable when no replica is running, and every run of
# this leaves one running for the next five minutes or so — which is how three
# consecutive runs each reported "not a cold start" and asked for the same wait
# that had just been spent. --cold waits for the platform to say zero rather
# than asking somebody to guess when it will.
#
# The recording is the point. This environment exists to be measured once and
# destroyed, so every figure here becomes unavailable the moment the resource
# group does — and a measurement that lives only in a terminal that has since
# been closed was not a measurement. The file it writes is the deliverable; the
# terminal output is a copy of it.
#
# It creates nothing in Azure and costs a few cents of tokens.

source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

COLD=false
[[ "${1:-}" == "--cold" ]] && COLD=true

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
# The registration is not prepared here, only used. Preparing it is token.sh's
# job and needs rights over an Entra object that a measurement has no business
# holding — and the pipeline that runs this has deliberately not been given them:
# it may deploy resources and it may not rewrite the application everything
# authenticates against.
APP_ID="$(api_app_id)"
TOKEN="$(api_token "$APP_ID")" || die "$(printf '%s\n' \
    "no token, so there is nothing to measure." \
    "" \
    "If this is the first run against this app registration, it has to be prepared" \
    "once by somebody who can change it:  ./scripts/azure/token.sh")"
printf '  acquired (not printed, and not written to the report)\n\n'

# ── Cold start ───────────────────────────────────────────────────────────────
#
# The application scales to zero, so the first request after an idle period pays
# for an image pull, a process start and a connection pool. That number is the
# one nobody publishes and everybody meets, and it is only available once: the
# second request cannot be a cold start however it is labelled.
APP="${AZURE_API_NAME:-ca-paimon-api-${ENVIRONMENT}}"

replica_count() {
    local count
    count="$(az containerapp replica list --name "$APP" --resource-group "$GROUP" \
        --query 'length(@)' -o tsv 2>/dev/null || printf '')"
    printf '%s' "${count:-0}"
}

if [[ "$COLD" == true ]]; then
    bold "▸ waiting for the application to scale to zero"
    printf '  KEDA takes about five minutes from the last request. Nothing is billed for the\n'
    printf '  application while this waits; the database is, as always.\n'
    # Twenty-four tries at thirty seconds: twelve minutes, comfortably past the
    # cooldown, and it stops as soon as the platform says zero rather than after
    # a fixed sleep somebody has to pick.
    for _ in $(seq 1 24); do
        [[ "$(replica_count)" == "0" ]] && break
        printf '.'
        sleep 30
    done
    printf '\n\n'
fi

bold "▸ replicas before the first request"
REPLICAS="$(replica_count)"
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
    record "${REPLICAS} replica(s) were already running, so this is *not* a cold start —"
    record "\`./scripts/azure/measure.sh --cold\` waits for the platform to scale to zero and then"
    record "measures one."
fi
record ""
printf '  %ss  HTTP %s\n\n' "$SECONDS_TAKEN" "$STATUS"

# Stop here if nothing answered. The first version of this did not, and the run
# spent twenty minutes proving the same thing six times: liveness touches nothing
# at all, so a 5xx or a timeout on it means there is no ready replica to route
# to, and every measurement after it would be a second copy of that fact.
#
# A 401 is different and not fatal here — liveness is unauthenticated, so a token
# problem shows up further down where it can be told apart from this.
case "$STATUS" in
    000|5*)
        record "Nothing after that: liveness touches no dependency, so a ${STATUS} here means the"
        record "request never reached a ready replica. Every later figure would have measured the"
        record "same thing again."
        record ""
        warn "The application is not serving. 240 seconds is the Container Apps ingress"
        warn "timeout, not the application being slow: the ingress had no ready replica."
        warn ""
        warn "  ./scripts/azure/diagnose.sh    what the revision and its logs say"
        die "stopped at the first request. The report so far is at ${REPORT#"$ROOT"/}"
        ;;
esac

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
record "**Ingestion**: HTTP ${STATUS} in ${SECONDS_TAKEN}s."
record ""
record "$(python3 "$SCRIPTS/measurement.py" ingestion < "$BODY" || true)"
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
record "**Expect no improvement**, and read a difference either way as noise. Redis holds an"
record "embedding cache, not an answer cache (ADR-0039): the question's embedding is served"
record "from memory the second time, and everything expensive — retrieval and a full"
record "generation — happens again. The first run of this said the gap was a semantic cache,"
record "which does not exist here; the second answer came back *slower* and said so."
record ""
record "What this pair does establish is that the answer is reproducible and that nothing in"
record "the path degrades on a second call."
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
WAITED=0
FIRST_SEEN=0
if [[ -n "$WORKSPACE" ]]; then
    # Asked repeatedly rather than once after a fixed minute.
    #
    # Application Insights ingestion lags somewhere between one and five minutes,
    # and the old single `sleep 60` sat inside that range — so an empty answer
    # meant either "the telemetry is broken" or "you asked too early", with no
    # way to tell which. Run 10 reported exactly that: zero, and no way to know
    # what it meant.
    #
    # Polling collapses the ambiguity and costs almost nothing, because it stops
    # as soon as the answer is stable. How long it took is recorded, which makes
    # this a measurement of ingestion latency rather than only a check.
    #
    # And it stops when the count stops *growing*, not when it stops being zero.
    # The first version of this broke on the first non-zero answer and reported
    # "1" for a run that had made fifteen requests — a true statement about when
    # a trace becomes readable, and a useless one about how many arrived. Two
    # consecutive equal answers is the cheapest thing that distinguishes "the
    # telemetry is here" from "the telemetry is arriving".
    PREVIOUS="0"
    for _ in $(seq 1 12); do
        sleep 30
        WAITED=$((WAITED + 30))
        TRACES="$(az monitor log-analytics query --workspace "$WORKSPACE" --analytics-query \
            "AppRequests | where TimeGenerated > ago(30m) | summarize n = count() | project n" \
            --query '[0].n' -o tsv 2>/dev/null || printf 'unknown')"
        if [[ "$TRACES" =~ ^[1-9] ]]; then
            if [[ "$FIRST_SEEN" -eq 0 ]]; then
                FIRST_SEEN="$WAITED"
            fi
            if [[ "$TRACES" == "$PREVIOUS" ]]; then
                break
            fi
        fi
        PREVIOUS="$TRACES"
        printf '.'
    done
    printf '\n'
fi

if [[ "$TRACES" =~ ^[1-9] ]]; then
    record "**Requests in Application Insights**: ${TRACES}, the first readable ${FIRST_SEEN}s"
    record "after it was made and the count settled by ${WAITED}s."
    record ""
    record "Those two are ingestion lag, not the application's latency. They are recorded"
    record "because they are the only thing here that says how long after a request the trace"
    record "of it can be read — which is what decides whether an environment that lives for"
    record "twenty minutes can be observed at all."
    record ""
else
    record "**Requests in Application Insights**: none, after ${WAITED}s of asking."
    record ""
    record "The collector is the first place to look, not the second: an exporter that cannot"
    record "authenticate is healthy and silent. So its own output is below rather than a"
    record "command to run — by the time anybody reads this the environment is destroyed, and"
    record "an instruction that needs it alive is not a diagnosis."
    record ""
    record '```'
    # Standard error kept, because the interesting case is the CLI refusing.
    record "$(timeout 60 az containerapp logs show --name "ca-paimon-otel-${ENVIRONMENT}" \
        --resource-group "$GROUP" --tail 40 2>&1 ||
        printf 'no collector logs through the CLI.')"
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
