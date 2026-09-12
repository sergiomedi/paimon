#!/usr/bin/env bash
# Why is the application not answering?
#
#   ./scripts/azure/diagnose.sh
#
# Wakes the app, then reads — in the order the answer is usually in — the
# revision's state, its replicas, the container's own logs and the platform's.
# Creates nothing, changes nothing, and costs one request.
#
# It exists because the first request ever made to this deployment came back
# **504 after 240 seconds**, and the four commands that explain that are neither
# obvious nor memorable. 240 seconds is the Container Apps ingress timeout rather
# than anything the application did: the ingress had no ready replica to route
# to, held the connection open for its full window, and gave up. The application
# may not have been running at all.
#
# The ordering matters more than the commands. With minReplicas at zero there is
# no replica to read logs from until something asks for one, so this sends a
# request *first* and reads afterwards — the other way round reports an empty
# revision and explains nothing.

source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

require_az
announce
load_outputs

APP="${AZURE_API_NAME:-ca-paimon-api-${ENVIRONMENT}}"
API="${AZURE_API_URL%/}"

bold "▸ waking it"
if [[ -n "$API" ]]; then
    # In the background and left to run: the point is to make Container Apps
    # start a replica, not to wait for an answer this script already expects not
    # to get.
    curl --silent --show-error --max-time 240 --output /dev/null \
        "${API}/api/v1/health/live" &
    WAKE=$!
    printf '  a request is in flight; giving it 45 seconds to start a replica\n\n'
    sleep 45
else
    WAKE=""
    warn "  no apiUrl in the deployment outputs, so nothing can be woken."
    printf '\n'
fi

bold "▸ the application"
az containerapp show --name "$APP" --resource-group "$GROUP" --query '{
    running: properties.runningStatus,
    image: properties.template.containers[0].image,
    targetPort: properties.configuration.ingress.targetPort,
    external: properties.configuration.ingress.external,
    minReplicas: properties.template.scale.minReplicas
}' -o json 2>/dev/null || warn "  no application named ${APP} in ${GROUP}"
printf '\n'

bold "▸ revisions"
# healthState and runningState are different questions and both are worth one
# column: a revision can be provisioned perfectly and have no healthy replica.
az containerapp revision list --name "$APP" --resource-group "$GROUP" --query '[].{
    name: name,
    active: properties.active,
    state: properties.runningState,
    health: properties.healthState,
    replicas: properties.replicas,
    created: properties.createdTime
}' -o table || true
printf '\n'

bold "▸ replicas, and the containers inside them"
# This is the line that usually says it. A container whose startup probe never
# passes shows as running with restartCount climbing, which is a different
# failure from one that cannot pull its image and a different one again from a
# process that exits immediately.
az containerapp replica list --name "$APP" --resource-group "$GROUP" --query '[].{
    replica: name,
    state: properties.runningState,
    containers: properties.containers[].{name: name, ready: ready, started: started, restarts: restartCount, state: runningStateDetails}
}' -o json || true
printf '\n'

bold "▸ what the application printed"
timeout 60 az containerapp logs show --name "$APP" --resource-group "$GROUP" \
    --container api --tail 100 ||
    warn "  no container logs. If there is no replica, there is nothing to read."
printf '\n'

bold "▸ what the platform printed"
# A different stream, and the one that names an image that cannot be pulled, a
# probe that keeps failing, or a replica the platform killed.
timeout 60 az containerapp logs show --name "$APP" --resource-group "$GROUP" \
    --type system --tail 50 ||
    warn "  no system logs through the CLI."
printf '\n'

[[ -n "$WAKE" ]] && wait "$WAKE" || true

bold "▸ how to read this"
printf '%s\n' \
    "  · restarts climbing, ready false     the startup probe never passes. It is wired to" \
    "                                       /api/v1/health/ready, which opens the database," \
    "                                       the cache and the model endpoint — so any one of" \
    "                                       those failing keeps the replica out of the" \
    "                                       ingress, and even /health/live then times out." \
    "  · no replica at all                  the image cannot be pulled, or the revision was" \
    "                                       never activated. The system logs name which." \
    "  · a process that exits immediately   configuration. The application validates its" \
    "                                       settings at startup and refuses to run on a bad" \
    "                                       one; the container log's last line says which." \
    "  · healthy, and still 504             the ingress target port does not match the port" \
    "                                       the process listens on." \
    ""
printf 'Log Analytics keeps all of it for longer than the CLI will show:\n'
printf '  WORKSPACE="$(az monitor log-analytics workspace show --resource-group %s \\\n' "$GROUP"
printf '    --workspace-name log-paimon-%s --query customerId -o tsv)"\n' "$ENVIRONMENT"
printf '  az monitor log-analytics query --workspace "$WORKSPACE" --analytics-query \\\n'
printf '    "ContainerAppConsoleLogs_CL | where ContainerAppName_s == '"'"'%s'"'"' \\\n' "$APP"
printf '     | project TimeGenerated, Log_s | order by TimeGenerated desc | take 200" -o table\n\n'
