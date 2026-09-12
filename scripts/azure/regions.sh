#!/usr/bin/env bash
# Which regions can this subscription actually deploy this into?
#
#   ./scripts/azure/regions.sh                       the usual candidates
#   ./scripts/azure/regions.sh northeurope uksouth   just these
#
# Creates nothing. It runs `az deployment sub validate` — this template, these
# parameters — once per region and reports the first reason each one says no.
#
# It asks Azure twice per region, because one question is not enough. `validate`
# checks the template, the providers, ordinary SKUs and whether the region is open
# — and returns success for a region with no model quota at all. The first version
# of this script reported ten usable regions for a subscription that could not
# deploy a chat model in any of them. So the quota list is read as well.
#
# This exists because choosing a region by reading documentation has now failed
# three times, each for a different reason, and each time the answer took a
# failed deployment to find:
#
#   * Sweden Central had the models in its catalogue and no quota for the
#     deployment type the template asked for.
#   * Sweden Central had no Basic search capacity either.
#   * West Europe had both, and then stopped accepting new customers.
#
# None of those are properties of the region. They are properties of this
# subscription in that region on this day, which is why the only trustworthy
# answer comes from asking Azure rather than from a table in a document. Ten
# seconds per region, and nothing is created.

source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

require_az

# European regions that carry the services this template needs. Not exhaustive,
# and deliberately not sorted by preference: the point of this script is that
# preference is not the deciding factor.
DEFAULT_REGIONS=(
    westeurope
    northeurope
    swedencentral
    francecentral
    germanywestcentral
    uksouth
    switzerlandnorth
    norwayeast
    polandcentral
    italynorth
    spaincentral
)

REGIONS=("$@")
[[ ${#REGIONS[@]} -gt 0 ]] || REGIONS=("${DEFAULT_REGIONS[@]}")

# announce() is not used here: it prints a single location, and the whole point
# of this script is that there is not one yet.
bold "environment   $ENVIRONMENT"
printf 'subscription  %s (%s)\n\n' \
    "$(az account show --query name -o tsv)" "$(az account show --query id -o tsv)"
printf 'Validating the real template in %d regions. Nothing is created.\n\n' "${#REGIONS[@]}"

AZURE_PAIMON_OPERATOR_ID="$(operator_principal_id)"
AZURE_PAIMON_OPERATOR_NAME="$(operator_principal_name)"
export AZURE_PAIMON_OPERATOR_ID AZURE_PAIMON_OPERATOR_NAME

# Resolved once. `bicep build-params` emits the parameter file and the compiled
# template separately, and every one of these four values is currently a template
# default rather than something the parameter file sets — so reading only the
# parameter file would report nothing at all.
PARAMS="$(mktemp)"
trap 'rm -f "$PARAMS"' EXIT
bicep_cli build-params "$INFRA/main.bicepparam" --stdout > "$PARAMS" 2>/dev/null \
    || die "could not compile the parameter file. Try: ./scripts/check.sh infrastructure"

bold "▸ what the template asks for"
python3 "$(dirname "${BASH_SOURCE[0]}")/model_quota.py" --parameters "$PARAMS" --list | sed 's/^/  /'
printf '\n'

USABLE=()

for region in "${REGIONS[@]}"; do
    printf '  %-22s' "$region"

    # The scripts export this, and an exported value wins over the parameter
    # file's default — which is the whole mechanism being exercised here.
    export AZURE_PAIMON_LOCATION="$region"

    if output=$(az deployment sub validate \
        --name "paimon-${ENVIRONMENT}-probe" \
        --location "$region" \
        --template-file "$INFRA/main.bicep" \
        --parameters "$INFRA/main.bicepparam" 2>&1); then
        # Validated. Now the question validate does not answer.
        if quota=$(az cognitiveservices usage list --location "$region" -o json 2>/dev/null |
            python3 "$(dirname "${BASH_SOURCE[0]}")/model_quota.py" \
                --parameters "$PARAMS" --usage - 2>&1); then
            printf '\033[32myes\033[0m\n'
            USABLE+=("$region")
        else
            printf '\033[33mno\033[0m   %s\n' "$(printf '%s' "$quota" | head -1)"
        fi
        continue
    fi

    # One line per region, so the table stays readable. The full error is a
    # validate away once a region is worth arguing with.
    case "$output" in
        *RequestDisallowedByAzure*|*locationineligible*)
            printf '\033[33mno\033[0m   not accepting new customers here\n'
            ;;
        *InsufficientQuota*)
            printf '\033[33mno\033[0m   no model quota for this deployment type\n'
            ;;
        *ResourcesForSkuUnavailable*|*SkuNotAvailable*)
            printf '\033[33mno\033[0m   no capacity for one of the SKUs\n'
            ;;
        *MissingSubscriptionRegistration*)
            printf '\033[33mno\033[0m   a resource provider is not registered\n'
            ;;
        *LocationNotAvailableForResourceType*)
            printf '\033[33mno\033[0m   a resource type does not exist in this region\n'
            ;;
        *)
            # Unrecognised, so say so rather than imply it was understood. The
            # first line of the message is usually enough to decide whether to
            # look closer.
            detail="$(printf '%s' "$output" | grep -oE '"message": "[^"]{0,110}"' | head -1 || true)"
            printf '\033[33mno\033[0m   %s\n' "${detail:-unrecognised error; run validate against this region to see it}"
            ;;
    esac
done

printf '\n'
if [[ ${#USABLE[@]} -eq 0 ]]; then
    warn "No region in that list will take this deployment today."
    warn "That is not normally a template problem. Two things worth checking:"
    warn "  · whether the subscription is a trial, which is what most of these"
    warn "    restrictions are actually about"
    warn "  · az cognitiveservices usage list --location <region> -o table"
    exit 1
fi

bold "▸ usable today"
printf '  %s\n' "${USABLE[@]}"
printf '\n'
printf 'To use one:\n'
printf '  export AZURE_PAIMON_LOCATION=%s\n' "${USABLE[0]}"
printf '  ./scripts/azure/preview.sh\n\n'
printf 'If it is going to be used more than once, change the default in\n'
printf 'scripts/azure/_common.sh and infrastructure/main.bicepparam rather than\n'
printf 'relying on an export: they have to agree, and a stale default there has\n'
printf 'already silently overridden a deliberate choice once.\n'
