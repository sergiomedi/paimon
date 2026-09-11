#!/usr/bin/env bash
# What this deployment would change, without changing it.
#
#   ./scripts/azure/preview.sh
#
# Run this before every deploy. It is not a formality: what-if is the only place
# a Delete shows up before it happens, and the resources in later phases of this
# project hold data.
#
# Two honest limitations, worth knowing before trusting the output:
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

PAIMON_AZURE_OPERATOR_ID="$(operator_principal_id)"
export PAIMON_AZURE_OPERATOR_ID

az deployment sub what-if \
    --name "paimon-${ENVIRONMENT}-preview" \
    --location "$LOCATION" \
    --template-file "$INFRA/main.bicep" \
    --parameters "$INFRA/main.bicepparam"
