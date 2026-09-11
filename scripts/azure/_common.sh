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
