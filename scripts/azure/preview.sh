#!/usr/bin/env bash
# What this deployment would change, without changing it.
#
#   ./scripts/azure/preview.sh
#
# Run this before every deploy. It is not a formality: what-if is the only place
# a Delete shows up before it happens, and the resources in later phases of this
# project hold data.
#
# It runs two checks, and they answer different questions. **Validation** asks
# whether this deployment is possible at all: quota, SKU availability, resource
# providers, property-level correctness. **what-if** asks what would change if it
# were. Only the second was here to begin with, and it happily predicted sixteen
# resources in a region that could not have created two of them.
#
# Two honest limitations of what-if, worth knowing before trusting the output:
#
#   * what-if cannot resolve reference() expressions, so properties that depend on
#     one are reported as changing when they are not. Expect noise around keys,
#     endpoints and anything read back from another resource.
#   * Azure applies its own defaults after the template is submitted, and those
#     show up here as deletions of properties nobody set. They are not deletions.
#
# Neither makes it useless. Both mean "read it", not "run it".

source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

require_az
announce

AZURE_PAIMON_OPERATOR_ID="$(operator_principal_id)"
AZURE_PAIMON_OPERATOR_NAME="$(operator_principal_name)"
export AZURE_PAIMON_OPERATOR_ID AZURE_PAIMON_OPERATOR_NAME
resolve_api_image

validate

bold "▸ what would change"
az deployment sub what-if \
    --name "paimon-${ENVIRONMENT}-preview" \
    --location "$LOCATION" \
    --template-file "$INFRA/main.bicep" \
    --parameters "$INFRA/main.bicepparam"
