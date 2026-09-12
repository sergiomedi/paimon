#!/usr/bin/env bash
# Can this environment be called at all, and with what?
#
#   ./scripts/azure/token.sh
#
# Prepares the app registration if it needs it, acquires a token for the deployed
# API, and reports what is inside it. Creates nothing in Azure beyond the two
# properties an app registration needs to be asked for a token — see
# scripts/azure/app_registration.py for what those are and why none of them is a
# default.
#
# **It does not print the token.** The claims are what answer the question; the
# token is a credential, and the output of these scripts gets pasted into
# terminals, issues and chat windows as a matter of course. measure.sh captures
# one the same way and never shows it either.
#
# This exists because the gap between "deployed" and "callable" swallowed an
# afternoon. Everything was green — the container ran, the schema was current,
# the health endpoint answered — and there was no way to make a single
# authenticated request, because an app registration created by `az ad app
# create` exposes nothing, pre-authorises nobody, and issues the token format
# this API refuses. Nothing reported any of that: the API answered 401, which is
# what it should answer, and says nothing about which of the four possible causes
# it was.

source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

require_az
announce

APP_ID="$(api_app_id)"
TENANT_ID="$(az account show --query tenantId -o tsv)"

printf 'application   %s\n' "$APP_ID"
printf 'audience      %s\n' "$AZURE_PAIMON_API_AUDIENCE"
printf 'tenant        %s\n\n' "$TENANT_ID"

ensure_app_registration "$APP_ID"

bold "▸ asking for a token"
TOKEN="$(api_token "$APP_ID")" || die "no token, so there is nothing to check."
printf '  got one\n\n'

bold "▸ what is in it"
# The token goes into the checker on standard input and comes back as claims.
# Deliberately not through a variable the shell will echo, a file on disk, or an
# argument that `ps` would show to every other process on the machine.
if printf '%s' "$TOKEN" | python3 "$SCRIPTS/claims.py" \
    --expect-audience "$AZURE_PAIMON_API_AUDIENCE" \
    --expect-tenant "$TENANT_ID"; then
    printf '\n'
    bold "▸ this deployment will accept it"
    printf 'Next: ./scripts/azure/measure.sh\n'
else
    printf '\n'
    die "the token is valid and this deployment will not accept it. See above."
fi
