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
az keyvault list-deleted --query "[?starts_with(name, 'kv-paimon')].{name:name, deleted:properties.deletionDate, purgeAfter:properties.scheduledPurgeDate}" -o table || true

printf '\n'
# This one is not a curiosity. Quota for Azure OpenAI includes a row named
# OpenAI.S0.AccountCount, which on a trial subscription is often 1 of 1 — one
# account, total — and a soft-deleted account still counts against it. A row here
# is the difference between redeploying and being told there is no quota for a
# model the subscription plainly has quota for.
bold "▸ soft-deleted Azure OpenAI accounts still holding the account quota"
az cognitiveservices account list-deleted \
    --query "[?starts_with(name, 'oai-paimon')].{name:name, location:location, group:resourceGroup}" \
    -o table || true

printf '\n'
bold "▸ what bills by the hour while it exists"
printf 'Ordered by what actually matters, which changed once the database arrived:\n\n'
printf '  ~0.25 EUR/h   PostgreSQL flexible server, General Purpose. It bills for\n'
printf '                existing, it is the largest line by an order of magnitude, and\n'
printf '                a month of it costs more than this phase has budget for.\n'
printf '  ~0.01 EUR/h   the OpenTelemetry collector, which does not scale to zero.\n'
printf '  ~0.15 EUR/day the container registry.\n'
printf '  ~0.10 EUR/h   Azure AI Search, but ONLY with searchSku=basic. The default is\n'
printf '                the free tier, which bills nothing for existing.\n'
printf '  per token     the model deployments, and the API while it serves. Both cost\n'
printf '                nothing idle; the API scales to zero.\n\n'
printf 'So an environment left running overnight costs a few euros, and the way to pay\n'
printf 'nothing is ./scripts/azure/destroy.sh rather than a quiet weekend.\n'
printf '\nSpend to date is in the portal under Cost Management; the CLI cannot read it on\n'
printf 'every subscription type, so it is deliberately not scripted here rather than\n'
printf 'scripted and wrong.\n'
