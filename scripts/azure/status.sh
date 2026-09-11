#!/usr/bin/env bash
# What exists right now, and what it is costing.
#
#   ./scripts/azure/status.sh
#
# The question this answers is "did I leave something running", and it looks
# across the whole subscription rather than only at this environment's resource
# group. That is the point: the expensive mistake is not forgetting to destroy the
# environment you were working in, it is forgetting the one you made last week
# under a different name.
#
# Soft-deleted key vaults are listed too. They cost nothing, but they hold their
# name, and a redeployment that silently recovers one is a confusing morning.

source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

require_az
announce

bold "▸ everything tagged application=paimon, anywhere in this subscription"
az resource list --tag application=paimon \
    --query "sort_by([].{name:name, type:type, group:resourceGroup, location:location}, &group)" \
    -o table || true

printf '\n'
bold "▸ resource groups that look like ours"
az group list --query "[?starts_with(name, 'rg-paimon')].{name:name, location:location, state:properties.provisioningState}" -o table

printf '\n'
bold "▸ soft-deleted key vaults holding a name"
az keyvault list-deleted --query "[?starts_with(name, 'kv-paimon')].{name:name, deleted:properties.deletionDate, purgeAfter:properties.scheduledPurgeDate}" -o table 2>/dev/null || true

printf '\n'
bold "▸ what bills by the hour while it exists"
printf 'On the default settings, almost nothing: the search service is on the free tier\n'
printf 'and the model deployments bill per token. Deploy with searchSku=basic and that\n'
printf 'changes — roughly 0.10 EUR an hour, whether or not anything queries it. That is\n'
printf 'the one to care about, because it bills for existing rather than for working.\n'
printf 'The container registry adds about 0.15 EUR a day. The model deployments bill per\n'
printf 'token and cost nothing while idle.\n'
printf '\nSpend to date is in the portal under Cost Management; the CLI cannot read it on\n'
printf 'every subscription type, so it is deliberately not scripted here rather than\n'
printf 'scripted and wrong.\n'
