#!/usr/bin/env bash
# Shared settings for the Azure scripts. Sourced, never run.
#
# Every value here can be overridden from the environment, and none of them are
# written into the repository: an environment is identified by a name you choose,
# in a subscription only you are signed in to.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
INFRA="$ROOT/infrastructure"
# This directory, so that the helpers here can reach the Python beside them
# whichever script sourced this and from wherever it was run.
SCRIPTS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# bicep_cli and have_bicep, defined once for this script and for check.sh.
source "$ROOT/scripts/_bicep.sh"

# Name of the environment. Everything is named and tagged after it, and every
# script acts on exactly one.
ENVIRONMENT="${AZURE_PAIMON_ENV:-dev}"
# Sweden Central, matching the default in main.bicepparam. They have to agree:
# these scripts export AZURE_PAIMON_LOCATION, and an exported value wins over the
# parameter file's default — so a stale default here silently overrode the one
# that was changed on evidence, and every deployment went to the wrong region.
#
# Third region in this phase, and the first chosen from a complete quota dump
# rather than from a document: it is the only one of the ten probed that holds
# both deployments this template creates, and it holds far more besides. West
# Europe, which it replaces, stopped accepting new customers outright.
LOCATION="${AZURE_PAIMON_LOCATION:-swedencentral}"
GROUP="rg-paimon-${ENVIRONMENT}"

# Name of the image repository inside the registry. One repository, many tags.
IMAGE_REPOSITORY="paimon-api"

export AZURE_PAIMON_ENV="$ENVIRONMENT"
export AZURE_PAIMON_LOCATION="$LOCATION"

bold() { printf '\033[1m%s\033[0m\n' "$1"; }
warn() { printf '\033[33m%s\033[0m\n' "$1"; }
die() { printf '\033[31m%s\033[0m\n' "$1" >&2; exit 1; }

require_az() {
    command -v az >/dev/null 2>&1 || die "the Azure CLI is not installed: https://aka.ms/azure-cli"
    az account show >/dev/null 2>&1 || die "not signed in. Run: az login"

    # Checked here so that no command further in can stop to ask for it.
    #
    # Every containerapp command needs this extension, and when it is missing the
    # CLI offers to install it — interactively, on standard error. A script that
    # sends that stream to /dev/null turns the question into an indefinite wait on
    # an answer the operator was never shown, which is exactly what happened to
    # the first run of bootstrap.sh: no output, no error, no prompt returned.
    #
    # Not installed silently. Installing software on somebody's machine from a
    # deployment script is not this script's business; naming the one command is.
    az extension show --name containerapp >/dev/null 2>&1 || die "$(printf '%s\n' \
        "the Azure CLI extension 'containerapp' is not installed." \
        "" \
        "  az extension add --name containerapp --allow-preview true" \
        "" \
        "Installed here rather than when a command first needs it, because the CLI" \
        "asks interactively and a script is not there to answer.")"
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
    if [[ -n "${AZURE_PAIMON_OPERATOR_ID:-}" ]]; then
        printf '%s' "$AZURE_PAIMON_OPERATOR_ID"
        return
    fi
    az ad signed-in-user show --query id -o tsv 2>/dev/null || printf ''
}

# The same person's sign-in name. The database administrator is recorded as an
# object id *and* a display name, and Azure refuses a create where the two
# disagree — with a message about the name, which sends you looking in the wrong
# place.
operator_principal_name() {
    if [[ -n "${AZURE_PAIMON_OPERATOR_NAME:-}" ]]; then
        printf '%s' "$AZURE_PAIMON_OPERATOR_NAME"
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
        *unable*to*pull*image*using*Managed*identity*)
            warn "  A container is attached to an identity that cannot pull from the registry."
            warn "  Only the workload identity holds AcrPull. A job running as another identity"
            warn "  has to be attached to both and pull as the workload's — which is what the"
            warn "  bootstrap job's registries[].identity names."
            ;;
        *InsufficientPrivilegeError*CREATE\ EXTENSION*|*isn\'t\ a\ trusted\ extension*)
            warn "  An ordinary role tried to create an extension. Only members of"
            warn "  azure_pg_admin may create an untrusted one — and Azure checks that before"
            warn "  PostgreSQL notices the extension is already installed, so IF NOT EXISTS"
            warn "  does not help. A migration must ask the catalogue first and skip."
            ;;
        *AadAuthPrincipalCreationFailed*42710*)
            warn "  PostgreSQL already has a role under a name that no longer matches the one"
            warn "  Azure is asking for. A role name stops at 63 characters, and Azure registers"
            warn "  an Entra administrator under its sign-in name — so a guest UPN longer than"
            warn "  that is created truncated and never matches again."
            warn ""
            warn "  registerOperatorAsAdministrator is off by default for exactly this reason."
            warn "  If it was turned on, turn it off: the administrator that does the work is the"
            warn "  management identity, and it is short by construction."
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
    if [[ -n "${AZURE_PAIMON_API_IMAGE:-}" ]]; then
        printf '%s' "$AZURE_PAIMON_API_IMAGE"
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
    AZURE_PAIMON_API_IMAGE="$(api_image)"
    if [[ -n "$AZURE_PAIMON_API_IMAGE" ]]; then
        AZURE_PAIMON_DEPLOY_API="true"
        printf 'image         %s\n\n' "$AZURE_PAIMON_API_IMAGE"
        # Checked here, on the pass that actually configures the application,
        # rather than by the template. An audience is baked into the container at
        # deployment time, so a wrong one is not a runtime misconfiguration you
        # can correct — it is an API that rejects every token until the next
        # deployment. The template can only refuse; this can explain.
        if [[ -z "${AZURE_PAIMON_API_AUDIENCE:-}" ]]; then
            warn "AZURE_PAIMON_API_AUDIENCE is not set, and this pass deploys the application."
            warn ""
            warn "It is the 'aud' claim the API will require, and it has to be the identifier"
            warn "URI of your Entra app registration. Microsoft's default tenant policy refuses"
            warn "a memorable one like api://paimon: it must contain the app id, the tenant id"
            warn "or a verified domain. So:"
            warn ""
            warn "  APP_ID=\"\$(az ad app list --display-name paimon-api --query '[0].appId' -o tsv)\""
            warn "  az ad app update --id \"\$APP_ID\" --identifier-uris \"api://\$APP_ID\""
            warn "  export AZURE_PAIMON_API_AUDIENCE=\"api://\$APP_ID\""
            die "nothing deployed."
        fi
    else
        AZURE_PAIMON_DEPLOY_API="false"
        # A value the template will not use, present only because the parameter
        # is required and a required parameter with a usable default is how an
        # unstartable app gets deployed by accident.
        AZURE_PAIMON_API_IMAGE="none"
        warn "No image published yet, so this pass leaves the application out."
        warn "Afterwards:  ./scripts/azure/publish.sh  &&  ./scripts/azure/deploy.sh"
        printf '\n'
    fi
    export AZURE_PAIMON_API_IMAGE AZURE_PAIMON_DEPLOY_API
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
    # Under `timeout`, because this command hangs. It waits on a log stream that
    # a finished execution never produces another line on, and the first real run
    # of the bootstrap sat at this line indefinitely on a job that had already
    # completed — the outcome was known and unreachable. Sixty seconds is far
    # longer than the call needs when it works.
    # Standard error is *not* discarded. Hiding it here is what hid the prompt
    # that hung the first real run: a question nobody could see, waiting for an
    # answer nobody knew to give. Noise on the terminal is a smaller problem than
    # a script that stops for a reason it refuses to print.
    if ! timeout 60 az containerapp job logs show --name "$job" --resource-group "$GROUP" \
        --container "$container" --execution "$execution" --tail 100; then
        warn "  no logs through the CLI. They are in Log Analytics a minute or two later:"
        warn ""
        warn "    WORKSPACE=\"\$(az monitor log-analytics workspace show \\"
        warn "      --resource-group $GROUP --workspace-name log-paimon-${ENVIRONMENT} \\"
        warn "      --query customerId -o tsv)\""
        warn "    az monitor log-analytics query --workspace \"\$WORKSPACE\" --analytics-query \\"
        warn "      \"ContainerAppConsoleLogs_CL | where ContainerName_s == '${container}'\\"
        warn "       | project TimeGenerated, Log_s | order by TimeGenerated asc\" -o table"
    fi

    printf '\n'
    JOB_STATUS="$status"
    JOB_EXECUTION="$execution"
}

# The deployment's own outputs, as written by deploy.sh.
#
# Sourced rather than re-queried: the file is regenerated on every deployment and
# reading it is instant, where `az deployment sub show` is a round trip for
# values that are already on disk. A script that needs it and cannot find it has
# nothing to work with, so it says which command produces it.
load_outputs() {
    local outputs="$INFRA/.env.${ENVIRONMENT}"
    [[ -f "$outputs" ]] || die "$(printf '%s\n' \
        "no deployment outputs at $outputs." \
        "" \
        "They are written by a successful deployment:" \
        "  ./scripts/azure/deploy.sh")"
    set -a
    # shellcheck disable=SC1090
    source "$outputs"
    set +a
}

# The app registration this deployment issues tokens against.
#
# Taken from the audience rather than looked up by display name: the audience is
# what the running container checks, so anything else would be a second source of
# truth for the same fact — and the failure that produces is a token that is
# perfectly valid for an application this API has never heard of.
api_app_id() {
    local audience="${AZURE_PAIMON_API_AUDIENCE:-}"
    [[ -n "$audience" ]] || die "$(printf '%s\n' \
        "AZURE_PAIMON_API_AUDIENCE is not set, and it is what identifies the app" \
        "registration to ask for a token." \
        "" \
        "  APP_ID=\"\$(az ad app list --display-name paimon-api --query '[0].appId' -o tsv)\"" \
        "  export AZURE_PAIMON_API_AUDIENCE=\"api://\$APP_ID\"")"
    printf '%s' "${audience#api://}"
}

# Make the app registration able to issue tokens for the Azure CLI, if it is not
# already. Idempotent, and silent when there is nothing to do.
#
# What it changes and why is in scripts/azure/app_registration.py. The short
# version: a resource with no delegated scope cannot be asked for a delegated
# token, consent for the Azure CLI has no owner to grant it, and a v1.0 token
# fails this API's issuer check.
ensure_app_registration() {
    local app_id="$1" object_id application patch
    object_id="$(az ad app show --id "$app_id" --query id -o tsv 2>/dev/null || printf '')"
    [[ -n "$object_id" ]] || die "$(printf '%s\n' \
        "no app registration with id ${app_id} in this tenant." \
        "" \
        "AZURE_PAIMON_API_AUDIENCE names one that does not exist here, which usually" \
        "means az login signed into a different directory than the one it was created in.")"

    # A service principal is the application's presence *in this tenant*. Without
    # one the registration exists and nothing can be issued against it, and the
    # error says the principal was not found in the directory — which reads as a
    # sign-in problem rather than as a missing object.
    az ad sp show --id "$app_id" -o none 2>/dev/null ||
        az ad sp create --id "$app_id" -o none

    # One change per pass, re-reading in between. Graph validates a
    # pre-authorised client against the scopes it has *already stored*, so the
    # obvious single request — add the scope and authorise the client for it —
    # is refused outright with a permission id that cannot be found. The scope
    # has to land first. app_registration.py therefore plans one step at a time,
    # and this applies them until it has nothing left to say.
    # Four passes for three steps: a scope, the pre-authorised client, and the
    # application role. One spare, because a pass that Graph has not yet made
    # visible costs a repeat rather than a failure.
    local announced=false
    for _ in 1 2 3 4; do
        application="$(az ad app show --id "$app_id" -o json)"
        patch="$(printf '%s' "$application" | python3 "$SCRIPTS/app_registration.py")"
        [[ -n "$patch" ]] || break

        if [[ "$announced" == false ]]; then
            bold "▸ preparing the app registration"
            announced=true
        fi
        az rest --method PATCH \
            --url "https://graph.microsoft.com/v1.0/applications/${object_id}" \
            --headers 'Content-Type=application/json' \
            --body "$patch" -o none ||
            die "$(printf '%s\n' \
                "Microsoft Graph refused that change to the app registration." \
                "" \
                "The message above is Graph's own. Nothing else was attempted, and the" \
                "registration is in whatever state the previous steps left it — this is safe" \
                "to run again once the reason is dealt with.")"
        # Read-after-write here is usually consistent and occasionally is not;
        # the next pass re-reads, and the scope's id is derived rather than
        # generated so a stale read cannot produce a second scope.
        sleep 10
    done

    if [[ "$announced" == true ]]; then
        printf '  a delegated scope, the Azure CLI pre-authorised, v2.0 tokens, and an\n'
        printf '  application role for callers that are not people\n\n'
    fi
}

# A token for the deployed API, printed on standard output and nowhere else.
#
# Callers capture it. Nothing in this repository prints a token to a terminal:
# these scripts' output is pasted into chats and issues routinely, and a bearer
# token that lands in one is a credential to rotate.
api_token() {
    local app_id="$1" token
    token="$(az account get-access-token --scope "api://${app_id}/.default" \
        --query accessToken -o tsv 2>&1)" || {
        printf '%s\n' "$token" >&2
        explain_token "$token"
        return 1
    }
    printf '%s' "$token"
}

# Turn a token acquisition failure into the sentence behind it.
explain_token() {
    local output="$1"
    case "$output" in
        *AADSTS65001*)
            warn "  Nobody has consented for the Azure CLI to call this API. That is what"
            warn "  pre-authorisation is for, and ensure_app_registration does it — but Graph"
            warn "  takes a minute to publish the change. Try again shortly."
            ;;
        *AADSTS500011*)
            warn "  The tenant has no service principal for this application. The registration"
            warn "  existing is not enough:  az ad sp create --id <app id>"
            ;;
        *AADSTS50105*|*AADSTS50020*)
            warn "  The signed-in account cannot be issued a token for this application. Check"
            warn "  which directory az login used, and that the account lives in it."
            ;;
        *AADSTS70011*|*invalid_scope*)
            warn "  The scope was refused. A resource with no delegated scope cannot be asked"
            warn "  for a delegated token at all — scripts/azure/token.sh adds one."
            ;;
    esac
}

# Which revision is serving, by suffix. Empty when there is no app, no revision,
# or nothing holding traffic.
#
# Asked of the platform rather than remembered anywhere. A file recording "what
# is live" is a second source of truth for something Azure already knows, and the
# moment it disagrees the disagreement is invisible — which matters most during a
# rollback, when being wrong about what is serving is the whole failure.
live_revision() {
    local app="${AZURE_API_NAME:-ca-paimon-api-${ENVIRONMENT}}" name
    name="$(az containerapp show --name "$app" --resource-group "$GROUP" \
        --query "properties.configuration.ingress.traffic[?weight==\`100\`].revisionName | [0]" \
        -o tsv 2>/dev/null || printf '')"
    # The suffix, not the full name: that is what the template takes and what a
    # revision is called in every command that addresses one.
    printf '%s' "${name#"${app}"--}"
}

# Hostname of one labelled revision, which is how a release tests a version that
# has no traffic. Three dashes, and they are not a typo: Container Apps builds
# label hostnames as <app>---<label>.<domain>.
labelled_url() {
    local label="$1" app="${AZURE_API_NAME:-ca-paimon-api-${ENVIRONMENT}}"
    local domain="${AZURE_CONTAINER_APPS_DEFAULT_DOMAIN:-}"
    [[ -n "$domain" ]] || return 1
    printf 'https://%s---%s.%s' "$app" "$label" "$domain"
}
