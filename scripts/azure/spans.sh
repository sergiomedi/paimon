#!/usr/bin/env bash
# What one request actually did, as the platform recorded it.
#
#   ./scripts/azure/spans.sh                  the most recent answer request
#   ./scripts/azure/spans.sh <operation id>   a particular one
#
# Prints the span tree of a single request out of Log Analytics: the request, and
# every dependency underneath it with its duration. Creates nothing and costs
# nothing.
#
# The portal's transaction view shows the same thing more prettily and cannot be
# committed. This can: the environment it describes is deleted within the hour,
# and a screenshot of it in somebody's downloads folder is not a measurement.
#
# It also answers a question the collector cannot. The collector's health endpoint
# reports that it is accepting telemetry, not that Application Insights received
# any — an exporter that cannot authenticate looks healthy and drops everything.
# A span tree here is proof that the whole chain works: application, collector,
# managed identity, workspace.

source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

require_az
announce
load_outputs

WORKSPACE="$(az monitor log-analytics workspace show --resource-group "$GROUP" \
    --workspace-name "log-paimon-${ENVIRONMENT}" --query customerId -o tsv 2>/dev/null || printf '')"
[[ -n "$WORKSPACE" ]] || die "no workspace log-paimon-${ENVIRONMENT} in ${GROUP}."

OPERATION="${1:-}"

if [[ -z "$OPERATION" ]]; then
    bold "▸ the most recent answer request"
    # Requests are named by route rather than by URL, so this matches the
    # answering endpoint without matching every health probe in the workspace.
    OPERATION="$(az monitor log-analytics query --workspace "$WORKSPACE" --analytics-query \
        "AppRequests
         | where TimeGenerated > ago(4h) and Name contains 'answer'
         | top 1 by TimeGenerated desc
         | project OperationId" \
        --query '[0].OperationId' -o tsv 2>/dev/null || printf '')"
    [[ -n "$OPERATION" ]] || die "$(printf '%s\n' \
        "no answer request in the last four hours." \
        "" \
        "Telemetry lags by a minute or two, and an empty result minutes after a request" \
        "is worth taking seriously — the collector is where to look:" \
        "  az containerapp logs show --name ca-paimon-otel-${ENVIRONMENT} --resource-group $GROUP --tail 100")"
    printf '  %s\n\n' "$OPERATION"
fi

bold "▸ the request"
az monitor log-analytics query --workspace "$WORKSPACE" --analytics-query \
    "AppRequests
     | where OperationId == '${OPERATION}'
     | project TimeGenerated, Name, DurationMs, ResultCode, Success" -o table

printf '\n'
bold "▸ what it did"
# Ordered by time rather than nested: a flat list in order is the shape a terminal
# can show honestly, and the durations are what anybody reads this for.
az monitor log-analytics query --workspace "$WORKSPACE" --analytics-query \
    "AppDependencies
     | where OperationId == '${OPERATION}'
     | order by TimeGenerated asc
     | project TimeGenerated, Type, Name, Target, DurationMs, Success" -o table

printf '\n'
bold "▸ and what it cost"
# The metrics the platform emits for itself. Absent when no price table was
# configured, which is deliberate: a model with no published price produces
# silence rather than a zero (see docs/deployment.md).
az monitor log-analytics query --workspace "$WORKSPACE" --analytics-query \
    "AppMetrics
     | where TimeGenerated > ago(4h) and Name startswith 'paimon'
     | summarize total = sum(Sum), n = count() by Name
     | order by Name asc" -o table

printf '\n'
printf 'Paste the two tables above into this session'"'"'s file in docs/measurements/. They are\n'
printf 'the end-to-end picture of one question, and they stop existing with the workspace.\n'
