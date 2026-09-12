#!/usr/bin/env bash
# Remove the environment, completely.
#
#   ./scripts/azure/destroy.sh             ask first
#   ./scripts/azure/destroy.sh --yes       do not ask
#
# This is the most important script in the directory, which is not a sentence
# anybody expects to write about a teardown. The deployment exists to be measured
# and then to stop existing: the budget for this whole phase is a fixed amount of
# trial credit, and an AI Search service nobody destroyed bills for every hour it
# is alive whether or not a single query reaches it.
#
# Deleting the resource group is not enough, and that is the part people discover
# on the second deployment rather than the first:
#
#   * A key vault is SOFT DELETED. Its name is held for the retention window, and
#     a redeployment either fails on the name or silently recovers the old vault
#     with whatever was in it.
#   * Cognitive Services accounts — Azure OpenAI — behave the same way, and also
#     hold their custom domain. Those arrive in a later batch; the purge is
#     already here so that it is not forgotten at the moment it starts mattering.
#
# So this deletes, then purges, then shows what is left.

source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

CONFIRM=true
[[ "${1:-}" == "--yes" ]] && CONFIRM=false

require_az
announce

if ! az group show --name "$GROUP" >/dev/null 2>&1; then
    warn "No resource group named $GROUP. Nothing to delete."
else
    bold "▸ what would be deleted"
    az resource list --resource-group "$GROUP" \
        --query "[].{name:name, type:type}" -o table

    # Asked before the confirmation, because the answer changes whether the
    # confirmation should be given at all. An environment destroyed without
    # having been measured cost money and produced nothing: everything it could
    # have told you goes with the resource group, and the only way back is
    # another deployment and another hour.
    if ! compgen -G "$ROOT/docs/measurements/${ENVIRONMENT}-*.md" >/dev/null; then
        printf '\n'
        warn "Nothing in docs/measurements was recorded from a '${ENVIRONMENT}' environment."
        warn "This environment exists to be measured. ./scripts/azure/measure.sh takes a few"
        warn "minutes and writes the numbers down; after this they are unobtainable."
    fi

    if [[ "$CONFIRM" == true ]]; then
        printf '\n'
        warn "This deletes the resource group and everything in it, permanently."
        read -r -p "Type the environment name (${ENVIRONMENT}) to confirm: " answer
        [[ "$answer" == "$ENVIRONMENT" ]] || die "nothing deleted."
    fi

    bold "▸ deleting $GROUP"
    # Not --no-wait: the purges below need the delete to have finished, and a
    # teardown that returns before it is torn down is how a bill survives it.
    az group delete --name "$GROUP" --yes
fi

# Purging is per-resource and cannot be done before the delete completes.
bold "▸ purging soft-deleted key vaults"
for vault in $(az keyvault list-deleted --query "[?starts_with(name, 'kv-paimon')].name" -o tsv 2>/dev/null); do
    printf '  %s\n' "$vault"
    az keyvault purge --name "$vault" --no-wait 2>/dev/null ||
        warn "  could not purge $vault — it may be protected, or already gone"
done

bold "▸ purging soft-deleted Azure OpenAI accounts"
while IFS=$'\t' read -r name group location; do
    [[ -n "$name" ]] || continue
    printf '  %s\n' "$name"
    az cognitiveservices account purge --name "$name" --resource-group "$group" --location "$location" 2>/dev/null ||
        warn "  could not purge $name"
done < <(az cognitiveservices account list-deleted \
    --query "[?starts_with(name, 'oai-paimon')].[name, resourceGroup, location]" \
    -o tsv 2>/dev/null || true)

rm -f "$INFRA/.env.${ENVIRONMENT}"

printf '\n'
bold "▸ what is left, anywhere in this subscription"
az resource list --tag application=paimon --query "[].{name:name, type:type, group:resourceGroup}" -o table

printf '\n'
printf 'An empty table above means this cost you nothing further. If it is not empty,\n'
printf 'run ./scripts/azure/status.sh — something is in a group this script did not own.\n'
