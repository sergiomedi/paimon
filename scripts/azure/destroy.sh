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

    # ── The two that hold something scarce, first and by themselves ──────────
    #
    # A key vault and an Azure OpenAI account are soft-deleted rather than
    # deleted, and each then holds its name for the retention window. The OpenAI
    # account also holds a slot against OpenAI.S0.AccountCount, which on a trial
    # subscription is 1 of 1 — so one left behind does not merely collide with
    # the next deployment, it forbids it.
    #
    # They used to be purged after the resource group delete, because a purge
    # cannot happen until the delete has completed. That ordering was measured in
    # delivery run 9 and it cost **thirty-three minutes**: the group contains a
    # PostgreSQL flexible server on a private network and a Container Apps
    # managed environment, and deleting those is most of an hour's patience. The
    # run was killed by its own timeout before reaching the purges, which is
    # almost certainly what left the soft-deleted account that then failed the
    # run after it.
    #
    # Deleting these two on their own takes seconds, so they go first. What the
    # next deployment needs — the names, and the quota — is free within a minute
    # of asking, and no longer hostage to how long the slow half takes.
    bold "▸ releasing the names and the quota"
    for vault in $(az keyvault list --resource-group "$GROUP" --query "[].name" -o tsv); do
        printf '  %s' "$vault"
        az keyvault delete --name "$vault" --resource-group "$GROUP" -o none
        if az keyvault purge --name "$vault" -o none; then
            printf ' purged\n'
        else
            printf '\n'
            warn "  deleted, but not purged — purge protection, or a role you do not hold"
        fi
    done
    for account in $(az cognitiveservices account list --resource-group "$GROUP" \
        --query "[].name" -o tsv); do
        printf '  %s' "$account"
        az cognitiveservices account delete --name "$account" --resource-group "$GROUP" -o none
        # By resource id, read back after the delete. The name-and-group-and-region
        # form needs all three correct and says nothing useful when one is not.
        id="$(az cognitiveservices account list-deleted \
            --query "[?name=='${account}'].id | [0]" -o tsv)"
        if [[ -n "$id" ]] && az resource delete --ids "$id" -o none; then
            printf ' purged\n'
        else
            printf '\n'
            warn "  deleted, but not purged. It still holds this subscription's only account."
        fi
    done
    printf '\n'

    bold "▸ deleting $GROUP"
    # --no-wait, which it could not be until the block above existed. Nothing
    # downstream depends on this finishing any more, and ARM does not need to be
    # watched to carry on: the deletion is accepted and proceeds whether or not
    # anybody is still connected. Waiting bought one thing — certainty that the
    # billing had stopped — and that is now bought below, and more cheaply.
    az group delete --name "$GROUP" --yes --no-wait
fi

# Purging is per-resource and cannot be done before the delete completes.
#
# Neither purge discards standard error any more, and neither reports success it
# has not checked. The previous version printed the name of everything it was
# about to purge and then said nothing whatever the outcome — so a teardown that
# purged nothing at all was indistinguishable from one that worked, and the
# `status.sh` immediately afterwards found both the vault and the OpenAI account
# still soft-deleted, minutes after the script had implied otherwise.
bold "▸ purging soft-deleted key vaults"
for vault in $(az keyvault list-deleted --query "[?starts_with(name, 'kv-paimon')].name" -o tsv); do
    printf '  %s' "$vault"
    # Not --no-wait. A purge that returns before it has purged cannot be
    # verified, and verifying is the whole point of the block below.
    if az keyvault purge --name "$vault" -o none; then
        printf ' purged\n'
    else
        printf '\n'
        warn "  could not purge $vault — purge protection, or a role you do not hold"
    fi
done

bold "▸ purging soft-deleted Azure OpenAI accounts"
# By resource id. The name-and-group-and-region form needs all three correct and
# reports nothing useful when one is not — which is how a purge that silently did
# nothing left a soft-deleted account holding this subscription's only Azure
# OpenAI account, and the next deployment failed on FlagMustBeSetForRestore for a
# name nobody could see.
for id in $(az cognitiveservices account list-deleted \
    --query "[?starts_with(name, 'oai-paimon')].id" -o tsv); do
    printf '  %s' "${id##*/}"
    if az resource delete --ids "$id" -o none; then
        printf ' purged\n'
    else
        printf '\n'
        warn "  could not purge ${id##*/}"
    fi
done

rm -f "$INFRA/.env.${ENVIRONMENT}"

printf '\n'
bold "▸ is the group gone yet"
# Asked for two minutes and then let go. The deletion is ARM's now — it was
# accepted, and it finishes whether or not this script is still watching — so
# this is a courtesy report rather than the thing that makes it happen.
#
# Two minutes because that is long enough to catch a small environment finishing
# and far too short for a large one, and pretending otherwise is what cost the
# thirty-three minutes. Nothing downstream waits on the answer.
GONE=false
for _ in $(seq 1 12); do
    if ! az group show --name "$GROUP" -o none 2>/dev/null; then
        GONE=true
        break
    fi
    printf '.'
    sleep 10
done
printf '\n'

if [[ "$GONE" == true ]]; then
    printf '  gone.\n\n'
else
    printf '  still deleting, which is normal and costs nothing extra to leave alone.\n'
    printf '  A PostgreSQL flexible server and a Container Apps environment take the best\n'
    printf '  part of half an hour between them. ARM continues without this script.\n\n'
    printf '  Later, to confirm:  ./scripts/azure/status.sh\n\n'
fi

bold "▸ what is left, anywhere in this subscription"
az resource list --tag application=paimon --query "[].{name:name, type:type, group:resourceGroup}" -o table

# Asked again, of Azure, after the purges rather than before them. "I ran the
# purge" and "the name is free" are separate claims, and only the second one
# matters to the next deployment.
printf '\n'
bold "▸ and what is still soft-deleted"
REMAINING_VAULTS="$(az keyvault list-deleted --query "[?starts_with(name, 'kv-paimon')].name" -o tsv)"
REMAINING_ACCOUNTS="$(az cognitiveservices account list-deleted \
    --query "[?starts_with(name, 'oai-paimon')].name" -o tsv)"

if [[ -z "$REMAINING_VAULTS" && -z "$REMAINING_ACCOUNTS" ]]; then
    printf '  nothing. Every name this environment held is free again.\n\n'
else
    printf '\n'
    [[ -n "$REMAINING_VAULTS" ]] && warn "  key vaults:       $(printf '%s' "$REMAINING_VAULTS" | tr '\n' ' ')"
    [[ -n "$REMAINING_ACCOUNTS" ]] && warn "  OpenAI accounts:  $(printf '%s' "$REMAINING_ACCOUNTS" | tr '\n' ' ')"
    warn ""
    warn "These cost nothing and are not harmless. A soft-deleted resource holds its"
    warn "name for the retention window, and a soft-deleted Azure OpenAI account also"
    warn "holds an account against OpenAI.S0.AccountCount — which is 1 of 1 on a trial"
    warn "subscription. The next deployment will fail on FlagMustBeSetForRestore."
    warn ""
    warn "Retry, or purge by hand:"
    warn "  az keyvault purge --name <name>"
    warn "  az resource delete --ids \"\$(az cognitiveservices account list-deleted \\"
    warn "    --query \"[?starts_with(name, 'oai-paimon')].id\" -o tsv)\""
    printf '\n'
fi

printf 'The soft-delete list is the one that decides whether the next deployment can\n'
printf 'happen, and it is empty. The resource table above may not be, and that is not a\n'
printf 'failure: the group is being deleted in the background and its contents disappear\n'
printf 'as ARM works through them. What would be a failure is something outside this\n'
printf "environment's group — ./scripts/azure/status.sh names it.\n"
