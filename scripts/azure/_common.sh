#!/usr/bin/env bash
# Shared settings for the Azure scripts. Sourced, never run.
#
# Every value here can be overridden from the environment, and none of them are
# written into the repository: an environment is identified by a name you choose,
# in a subscription only you are signed in to.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
INFRA="$ROOT/infrastructure"

# Name of the environment. Everything is named and tagged after it, and every
# script acts on exactly one.
ENVIRONMENT="${PAIMON_AZURE_ENV:-dev}"
LOCATION="${PAIMON_AZURE_LOCATION:-swedencentral}"
GROUP="rg-paimon-${ENVIRONMENT}"

export PAIMON_AZURE_ENV="$ENVIRONMENT"
export PAIMON_AZURE_LOCATION="$LOCATION"

bold() { printf '\033[1m%s\033[0m\n' "$1"; }
warn() { printf '\033[33m%s\033[0m\n' "$1"; }
die() { printf '\033[31m%s\033[0m\n' "$1" >&2; exit 1; }

require_az() {
    command -v az >/dev/null 2>&1 || die "the Azure CLI is not installed: https://aka.ms/azure-cli"
    az account show >/dev/null 2>&1 || die "not signed in. Run: az login"
}

# The subscription being spent. Printed by every script before it does anything,
# because "which subscription am I in" is the question behind most of the
# expensive mistakes available here.
announce() {
    local name id
    name="$(az account show --query name -o tsv)"
    id="$(az account show --query id -o tsv)"
    bold "environment   $ENVIRONMENT"
    printf 'subscription  %s (%s)\n' "$name" "$id"
    printf 'location      %s\n' "$LOCATION"
    printf 'group         %s\n\n' "$GROUP"
}

# Object id of whoever is signed in, so the deployment can grant them the
# data-plane roles a person needs. Empty rather than fatal when it cannot be
# read: a service principal in a pipeline has no signed-in user, and that is a
# valid way to deploy.
operator_principal_id() {
    if [[ -n "${PAIMON_AZURE_OPERATOR_ID:-}" ]]; then
        printf '%s' "$PAIMON_AZURE_OPERATOR_ID"
        return
    fi
    az ad signed-in-user show --query id -o tsv 2>/dev/null || printf ''
}

# The same person's sign-in name. The database administrator is recorded as an
# object id *and* a display name, and Azure refuses a create where the two
# disagree — with a message about the name, which sends you looking in the wrong
# place.
operator_principal_name() {
    if [[ -n "${PAIMON_AZURE_OPERATOR_NAME:-}" ]]; then
        printf '%s' "$PAIMON_AZURE_OPERATOR_NAME"
        return
    fi
    az ad signed-in-user show --query userPrincipalName -o tsv 2>/dev/null || printf ''
}

# Ask Azure whether this deployment *can* happen, before asking what it would
# change. The two are different questions and only one of them was being asked:
# what-if predicted sixteen resources in a region that had no quota for the
# embedding model and no capacity for a search service, and said so nowhere.
# Preflight validation catches both, creates nothing, and takes seconds.
validate() {
    bold "▸ can this be deployed here?"
    local output
    if output=$(az deployment sub validate \
        --name "paimon-${ENVIRONMENT}-validate" \
        --location "$LOCATION" \
        --template-file "$INFRA/main.bicep" \
        --parameters "$INFRA/main.bicepparam" 2>&1); then
        printf '  yes\n\n'
        return 0
    fi

    printf '\n'
    # The useful part of an ARM error is buried several levels into a JSON blob
    # that the CLI prints as one line.
    printf '%s' "$output" | grep -oE '"(code|message)":"[^"]{0,300}"' | sed 's/^/  /' | head -12
    printf '\n'
    case "$output" in
        *InsufficientQuota*)
            warn "  No quota for that model in this deployment type and region. All three matter."
            warn "  az cognitiveservices usage list --location $LOCATION -o table"
            ;;
        *ResourcesForSkuUnavailable*)
            warn "  Azure has no capacity for that SKU in $LOCATION right now. Try another region."
            ;;
        *MissingSubscriptionRegistration*)
            warn "  A resource provider is not registered. The message above names it:"
            warn "  az provider register --namespace <the one it named>"
            ;;
    esac
    die "nothing deployed."
}
