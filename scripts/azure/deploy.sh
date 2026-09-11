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

PAIMON_AZURE_OPERATOR_ID="$(operator_principal_id)"
export PAIMON_AZURE_OPERATOR_ID
if [[ -z "$PAIMON_AZURE_OPERATOR_ID" ]]; then
    warn "No signed-in user object id: nobody will be granted access to the key vault."
    warn "That is correct for a pipeline and wrong for a laptop. Set PAIMON_AZURE_OPERATOR_ID to override."
    printf '\n'
fi

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
az deployment sub create \
    --name "$DEPLOYMENT" \
    --location "$LOCATION" \
    --template-file "$INFRA/main.bicep" \
    --parameters "$INFRA/main.bicepparam" \
    --output none

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
printf 'Nothing in this environment bills by the hour yet except the registry, at roughly\n'
printf '0.15 EUR a day. When you are finished: ./scripts/azure/destroy.sh\n'
