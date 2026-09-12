#!/usr/bin/env bash
# Shared settings for the Azure scripts. Sourced, never run.
#
# Every value here can be overridden from the environment, and none of them are
# written into the repository: an environment is identified by a name you choose,
# in a subscription only you are signed in to.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
INFRA="$ROOT/infrastructure"

# bicep_cli and have_bicep, defined once for this script and for check.sh.
source "$ROOT/scripts/_bicep.sh"

# Name of the environment. Everything is named and tagged after it, and every
# script acts on exactly one.
ENVIRONMENT="${PAIMON_AZURE_ENV:-dev}"
# Sweden Central, matching the default in main.bicepparam. They have to agree:
# these scripts export PAIMON_AZURE_LOCATION, and an exported value wins over the
# parameter file's default — so a stale default here silently overrode the one
# that was changed on evidence, and every deployment went to the wrong region.
#
# Third region in this phase, and the first chosen from a complete quota dump
# rather than from a document: it is the only one of the ten probed that holds
# both deployments this template creates, and it holds far more besides. West
# Europe, which it replaces, stopped accepting new customers outright.
LOCATION="${PAIMON_AZURE_LOCATION:-swedencentral}"
GROUP="rg-paimon-${ENVIRONMENT}"

# Name of the image repository inside the registry. One repository, many tags.
IMAGE_REPOSITORY="paimon-api"

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
    # that the CLI prints as one line, so this digs it out.
    #
    # `|| true`, and the result kept in a variable rather than piped straight to
    # the terminal, because of a bug this had: under `set -euo pipefail` a grep
    # that matches nothing exits 1 and kills the script *there* — before the
    # explanation, before the hint, before `die`. A validation failure printed a
    # heading, no error, and an empty prompt. The one code path whose entire job
    # is explaining a failure was the one that could not survive an unexpected
    # one.
    local detail
    detail="$(printf '%s' "$output" | grep -oE '"(code|message)":"[^"]{0,300}"' || true)"
    if [[ -n "$detail" ]]; then
        printf '%s\n' "$detail" | sed 's/^/  /'
    else
        # Not the shape expected. Print it whole rather than print nothing: an
        # error nobody recognises is exactly the one worth seeing verbatim.
        printf '%s\n' "$output" | sed 's/^/  /'
    fi
    printf '\n'
    explain "$output"
    die "nothing deployed."
}

# Turn an Azure error into the sentence somebody can act on.
#
# Shared by validate() and by deploy.sh, because a deployment fails for the same
# reasons a validation does — and the first real deployment of this template
# failed on two errors that validate() would have explained and `az deployment
# create` printed as one line of JSON.
explain() {
    local output="$1"
    case "$output" in
        *RequestDisallowedByAzure*|*locationineligible*)
            warn "  Azure is not accepting new customers in $LOCATION. This is not quota and"
            warn "  not a template problem: the region is closed to this subscription today."
            warn "  ./scripts/azure/regions.sh   — which regions will take it"
            ;;
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
        *FlagMustBeSetForRestore*)
            warn "  A soft-deleted account still holds one of these names, and on a trial"
            warn "  subscription OpenAI.S0.AccountCount is often 1 of 1 — so it also holds the"
            warn "  only account you are allowed. Purging it is the fix, not restoring it:"
            warn ""
            warn "    az cognitiveservices account list-deleted -o table"
            warn "    az resource delete --ids \"\$(az cognitiveservices account list-deleted \\"
            warn "      --query \"[?starts_with(name, 'oai-paimon')].id\" -o tsv)\""
            ;;
        *AadAuthOperationCannotBePerformedWhenServerIsNotAccessible*)
            warn "  The database was created but was not ready for a Microsoft Entra"
            warn "  administrator yet. This is a race Azure has had open since 2023: the server"
            warn "  reports Succeeded before it can accept principal operations, so dependsOn is"
            warn "  satisfied too early. It is not a template error and nothing is broken."
            warn ""
            warn "  Run the same command again. This deployment is idempotent, the server"
            warn "  already exists, and the second pass finds it ready."
            ;;
    esac
}

# Name of this environment's container registry. Empty when the environment does
# not exist yet, which is the case publish.sh has to report rather than trip over.
registry_name() {
    az acr list --resource-group "$GROUP" --query "[0].name" -o tsv 2>/dev/null || printf ''
}

# The image to deploy: whatever was asked for, or the newest tag in the registry.
#
# Resolved rather than defaulted. A Container App pointed at an image that does
# not exist deploys successfully and then fails every revision, so the failure
# arrives minutes later as an unhealthy app rather than immediately as a refused
# deployment. Asking the registry first turns that into one legible message.
api_image() {
    if [[ -n "${PAIMON_API_IMAGE:-}" ]]; then
        printf '%s' "$PAIMON_API_IMAGE"
        return
    fi

    local registry tag
    registry="$(registry_name)"
    if [[ -z "$registry" ]]; then
        printf ''
        return
    fi

    # Ordered by the time the manifest was written, newest first. Not by tag name:
    # the tags here are commit hashes, and those do not sort chronologically.
    tag="$(az acr repository show-tags --name "$registry" --repository "$IMAGE_REPOSITORY" \
        --orderby time_desc --top 1 -o tsv 2>/dev/null || printf '')"
    [[ -n "$tag" ]] || { printf ''; return; }

    printf '%s.azurecr.io/%s:%s' "$registry" "$IMAGE_REPOSITORY" "$tag"
}

# Resolve the image and decide whether the application can be deployed at all.
#
# The first deployment of a new environment cannot include the application: the
# registry it pulls from is created *by* that deployment, so there is nowhere for
# an image to have been pushed to yet. Rather than paper over that with a
# placeholder image — which produces an app that exists and never starts — the
# application is simply left out of the first pass and added by the second.
#
# This is the same shape as `azd provision` followed by `azd deploy`, for the
# same reason, and it is why the sequence in the guide is three commands.
resolve_api_image() {
    PAIMON_API_IMAGE="$(api_image)"
    if [[ -n "$PAIMON_API_IMAGE" ]]; then
        PAIMON_DEPLOY_API="true"
        printf 'image         %s\n\n' "$PAIMON_API_IMAGE"
        # Checked here, on the pass that actually configures the application,
        # rather than by the template. An audience is baked into the container at
        # deployment time, so a wrong one is not a runtime misconfiguration you
        # can correct — it is an API that rejects every token until the next
        # deployment. The template can only refuse; this can explain.
        if [[ -z "${PAIMON_AZURE_API_AUDIENCE:-}" ]]; then
            warn "PAIMON_AZURE_API_AUDIENCE is not set, and this pass deploys the application."
            warn ""
            warn "It is the 'aud' claim the API will require, and it has to be the identifier"
            warn "URI of your Entra app registration. Microsoft's default tenant policy refuses"
            warn "a memorable one like api://paimon: it must contain the app id, the tenant id"
            warn "or a verified domain. So:"
            warn ""
            warn "  APP_ID=\"\$(az ad app list --display-name paimon-api --query '[0].appId' -o tsv)\""
            warn "  az ad app update --id \"\$APP_ID\" --identifier-uris \"api://\$APP_ID\""
            warn "  export PAIMON_AZURE_API_AUDIENCE=\"api://\$APP_ID\""
            die "nothing deployed."
        fi
    else
        PAIMON_DEPLOY_API="false"
        # A value the template will not use, present only because the parameter
        # is required and a required parameter with a usable default is how an
        # unstartable app gets deployed by accident.
        PAIMON_API_IMAGE="none"
        warn "No image published yet, so this pass leaves the application out."
        warn "Afterwards:  ./scripts/azure/publish.sh  &&  ./scripts/azure/deploy.sh"
        printf '\n'
    fi
    export PAIMON_API_IMAGE PAIMON_DEPLOY_API
}

# Start a manually-triggered container apps job, wait for it, print its logs, and
# leave the outcome in JOB_STATUS.
#
# Two jobs now need this and a copy would drift: the bootstrap and the migration
# differ only in which container's logs to read and in what to say when it fails.
#
# Usage: run_job <job name> <container name>
run_job() {
    local job="$1" container="$2" execution status

    az containerapp job show --name "$job" --resource-group "$GROUP" -o none 2>/dev/null \
        || die "no job named ${job} in ${GROUP}. Deploy the environment first: ./scripts/azure/deploy.sh"

    bold "▸ starting ${job}"
    execution="$(az containerapp job start --name "$job" --resource-group "$GROUP" \
        --query name -o tsv)"
    printf 'execution    %s\n\n' "$execution"

    bold "▸ waiting"
    # `job start` has no --wait, so this polls. Thirty tries at ten seconds covers
    # the five minutes a cold image pull and the work itself take between them;
    # each job's own replicaTimeout is the real limit.
    status="Running"
    for _ in $(seq 1 30); do
        status="$(az containerapp job execution show --name "$job" --resource-group "$GROUP" \
            --job-execution-name "$execution" --query properties.status -o tsv 2>/dev/null || echo Unknown)"
        [[ "$status" == "Running" || "$status" == "Unknown" ]] || break
        printf '.'
        sleep 10
    done
    printf '\n\n'

    bold "▸ logs"
    # Ingestion lags the execution by a few seconds, which is long enough that a
    # job that has just finished often has nothing to show yet.
    sleep 5
    az containerapp job logs show --name "$job" --resource-group "$GROUP" \
        --container "$container" --execution "$execution" --tail 100 2>/dev/null \
        || warn "  no logs yet. Try again in a moment, or look in Log Analytics."

    printf '\n'
    JOB_STATUS="$status"
    JOB_EXECUTION="$execution"
}
