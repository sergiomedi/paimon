#!/usr/bin/env bash
# Create or update the Azure environment.
#
#   ./scripts/azure/deploy.sh              deploy, after showing what changes
#   ./scripts/azure/deploy.sh --yes        deploy without the confirmation
#
# Idempotent: names are derived from the subscription and the environment name,
# so running this twice updates rather than duplicates. That is also why
# destroy.sh has to purge soft-deleted resources — the second deployment wants
# the same names the first one used.
#
# The outputs are written to infrastructure/.env.<environment>, which is
# git-ignored. Later steps read it rather than asking you to copy values around.

source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

CONFIRM=true
[[ "${1:-}" == "--yes" ]] && CONFIRM=false

require_az
announce

AZURE_PAIMON_OPERATOR_ID="$(operator_principal_id)"
AZURE_PAIMON_OPERATOR_NAME="$(operator_principal_name)"
export AZURE_PAIMON_OPERATOR_ID AZURE_PAIMON_OPERATOR_NAME
if [[ -z "$AZURE_PAIMON_OPERATOR_ID" ]]; then
    warn "No signed-in user object id: nobody will be granted access to the key vault."
    warn "That is correct for a pipeline and wrong for a laptop. Set AZURE_PAIMON_OPERATOR_ID to override."
    printf '\n'
fi

resolve_api_image

# Whatever is serving keeps serving. A deployment is not a release: a release is
# release.sh, which puts a revision in with no traffic, checks it, and shifts
# weight deliberately. Without this line an ordinary deployment — a setting, a
# scale limit, anything — would hand the traffic to the newest revision as a side
# effect, which is a release nobody asked for and a rollback nobody noticed.
#
# Empty on a new environment, where "the newest" is the only revision there is.
if [[ -z "${AZURE_PAIMON_API_TRAFFIC_REVISION:-}" ]]; then
    AZURE_PAIMON_API_TRAFFIC_REVISION="$(live_revision)"
    export AZURE_PAIMON_API_TRAFFIC_REVISION
fi
[[ -n "$AZURE_PAIMON_API_TRAFFIC_REVISION" ]] &&
    printf 'traffic       stays on %s\n\n' "$AZURE_PAIMON_API_TRAFFIC_REVISION"

validate

if [[ "$CONFIRM" == true ]]; then
    bold "▸ what would change"
    az deployment sub what-if \
        --name "paimon-${ENVIRONMENT}-preview" \
        --location "$LOCATION" \
        --template-file "$INFRA/main.bicep" \
        --parameters "$INFRA/main.bicepparam"

    printf '\n'
    read -r -p "Deploy this to ${ENVIRONMENT}? [y/N] " answer
    [[ "$answer" == "y" || "$answer" == "Y" ]] || die "nothing deployed."
fi

# Named after the moment it ran. Deployment history is kept per subscription and
# is the only record of what was applied and when; a fixed name overwrites it.
DEPLOYMENT="paimon-${ENVIRONMENT}-$(date -u +%Y%m%d-%H%M%S)"

bold "▸ deploying ${DEPLOYMENT}"
# Captured rather than streamed, so that a failure can be explained instead of
# being handed over as one line of JSON. A deployment fails for the same reasons a
# validation does, and the first real run of this template proved it by failing on
# two errors that validate() already knew how to describe.
if ! OUTPUT=$(az deployment sub create \
    --name "$DEPLOYMENT" \
    --location "$LOCATION" \
    --template-file "$INFRA/main.bicep" \
    --parameters "$INFRA/main.bicepparam" \
    --output none 2>&1); then
    printf '\n'
    printf '%s\n' "$OUTPUT" | tr ',' '\n' | grep -E '"(code|message)"' | sed 's/^ */  /' | head -20 || true
    printf '\n'
    explain "$OUTPUT"
    printf '\n'
    warn "Deployment history keeps the detail:"
    warn "  az deployment operation sub list --name $DEPLOYMENT \\"
    warn "    --query \"[?properties.provisioningState=='Failed']\" -o json"
    die "deployment failed."
fi

OUTPUTS="$INFRA/.env.${ENVIRONMENT}"
bold "▸ writing $OUTPUTS"

# Deployment outputs as shell assignments. Nothing secret is in them, and nothing
# secret should ever be: an ARM output is stored in the deployment history in
# plaintext and readable by anybody with read access to the subscription.
az deployment sub show --name "$DEPLOYMENT" --query properties.outputs -o json |
    python3 "$(dirname "${BASH_SOURCE[0]}")/outputs.py" > "$OUTPUTS"

cat "$OUTPUTS"

printf '\n'
bold "▸ deployed"

if [[ "$AZURE_PAIMON_DEPLOY_API" == "false" ]]; then
    printf 'The application was not part of this pass, because there was no image to run.\n'
    printf 'Next:\n'
    printf '  ./scripts/azure/publish.sh      build the image into this registry\n'
    printf '  ./scripts/azure/deploy.sh       deploy it\n\n'
else
    printf 'Next, in this order:\n'
    printf '  ./scripts/azure/bootstrap.sh    the database role and extensions, once\n'
    printf '  ./scripts/azure/migrate.sh      bring the schema up to date\n\n'
    printf 'Neither is optional on a new environment, and the order matters: the migration\n'
    printf 'authenticates as the workload, which has no PostgreSQL role until the bootstrap\n'
    printf 'creates one. Give role assignments a few minutes to propagate first — a bootstrap\n'
    printf 'that fails on timing alone is safe to retry, and usually that is the whole fix.\n\n'
fi

printf 'This environment bills for the database at roughly 0.25 EUR an hour whether or not\n'
printf 'anything queries it. When you are finished: ./scripts/azure/destroy.sh\n'
