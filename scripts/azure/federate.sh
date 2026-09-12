#!/usr/bin/env bash
# Let GitHub Actions deploy this, without giving GitHub a secret.
#
#   ./scripts/azure/federate.sh sergiomedi/paimon
#
# Creates — or brings up to date — the app registration the pipeline signs in as,
# the federated credentials that let exactly this repository use it, and the role
# assignments it needs. Prints the three values to put in the repository's
# variables. **None of them is a secret**, which is the entire point.
#
# Run once per subscription, by a person. Everything else in Phase 8 is a
# workflow; this is the one prerequisite that is not, for the same reason the app
# registration in Phase 7 was not: an Entra object is not an ARM resource.
#
# ── Why OpenID Connect rather than a client secret ───────────────────────────
#
# The usual shape is `az ad sp create-for-rbac --sdk-auth`, whose output goes
# into a repository secret and is a password with a one-year life that nothing
# rotates. Phase 7 removed every password from this platform — the models, the
# search service, the database — and putting one back in the repository that
# deploys it would undo that at the front door.
#
# With federation there is nothing to store. GitHub mints a short-lived token per
# run, Entra is told in advance which repository, branch and environment it will
# trust, and a token minted for anything else is refused. The subject claim is
# the whole mechanism: a pull request from a fork cannot obtain the credential
# that deploys, because its subject is not one of the ones below.

source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

require_az

REPOSITORY="${1:-}"
[[ -n "$REPOSITORY" && "$REPOSITORY" == */* ]] || die "$(printf '%s\n' \
    "usage: ./scripts/azure/federate.sh <owner>/<repository>" \
    "" \
    "The repository is part of what Entra trusts, so it is an argument rather" \
    "than a default: a credential federated to the wrong repository is a" \
    "credential somebody else's pipeline can use.")"

APPLICATION_NAME="${AZURE_PAIMON_PIPELINE_APP:-paimon-pipeline}"
SUBSCRIPTION="$(az account show --query id -o tsv)"
TENANT="$(az account show --query tenantId -o tsv)"

#: The trigger this credential is for, and the subject GitHub will present.
#:
#: One per line, name and subject, because Entra matches the subject exactly and
#: a run whose subject is not listed gets no token at all. That exactness is the
#: security property: `pull_request` is deliberately absent, so a fork's pull
#: request cannot deploy anything, and `environment:production` is separate so
#: that promotion needs the approval GitHub attaches to that environment.
CREDENTIALS=(
    "paimon-main:repo:${REPOSITORY}:ref:refs/heads/main"
    "paimon-production:repo:${REPOSITORY}:environment:production"
)

bold "environment   pipeline identity"
printf 'subscription  %s\n' "$SUBSCRIPTION"
printf 'tenant        %s\n' "$TENANT"
printf 'repository    %s\n\n' "$REPOSITORY"

bold "▸ the application"
APP_ID="$(az ad app list --display-name "$APPLICATION_NAME" --query '[0].appId' -o tsv)"
if [[ -z "$APP_ID" ]]; then
    APP_ID="$(az ad app create --display-name "$APPLICATION_NAME" --query appId -o tsv)"
    printf '  created %s\n' "$APP_ID"
else
    printf '  exists  %s\n' "$APP_ID"
fi

# The registration is the definition; the service principal is its presence in
# this tenant, and a role cannot be assigned to something that is not present.
OBJECT_ID="$(az ad sp show --id "$APP_ID" --query id -o tsv 2>/dev/null || printf '')"
if [[ -z "$OBJECT_ID" ]]; then
    OBJECT_ID="$(az ad sp create --id "$APP_ID" --query id -o tsv)"
    printf '  service principal created\n'
fi
printf '\n'

bold "▸ federated credentials"
EXISTING="$(az ad app federated-credential list --id "$APP_ID" --query '[].name' -o tsv)"
for entry in "${CREDENTIALS[@]}"; do
    name="${entry%%:*}"
    subject="${entry#*:}"
    if printf '%s\n' "$EXISTING" | grep -qx "$name"; then
        printf '  %-20s exists\n' "$name"
        continue
    fi
    az ad app federated-credential create --id "$APP_ID" --parameters "$(printf '%s' "{
        \"name\": \"${name}\",
        \"issuer\": \"https://token.actions.githubusercontent.com\",
        \"subject\": \"${subject}\",
        \"audiences\": [\"api://AzureADTokenExchange\"]
    }")" -o none
    printf '  %-20s created  %s\n' "$name" "$subject"
done
printf '\n'

bold "▸ roles"
# Two, and the second one surprises people. Contributor deploys resources;
# creating a *role assignment* is a separate right, and this template creates
# several — every one of the platform's identities is granted what it needs by
# the deployment rather than by hand. Without the second role the deployment
# fails partway through, having created most of an environment.
#
# Role Based Access Control Administrator rather than Owner or User Access
# Administrator: it grants exactly "may create role assignments" and nothing
# else, which is the narrowest of the three that works.
for role in "Contributor" "Role Based Access Control Administrator"; do
    if az role assignment list --assignee "$APP_ID" --role "$role" \
        --scope "/subscriptions/${SUBSCRIPTION}" --query '[0].id' -o tsv | grep -q .; then
        printf '  %-42s already assigned\n' "$role"
        continue
    fi
    az role assignment create --assignee "$APP_ID" --role "$role" \
        --scope "/subscriptions/${SUBSCRIPTION}" -o none
    printf '  %-42s assigned\n' "$role"
done
printf '\n'

bold "▸ put these in the repository, as variables rather than secrets"
printf 'None of them is a credential: an application id, a tenant id and a subscription id\n'
printf 'are identifiers, and holding all three gets nobody a token. The token comes from\n'
printf 'GitHub, for a run whose subject Entra was told about above.\n\n'
printf '  gh variable set AZURE_CLIENT_ID       --body %s\n' "$APP_ID"
printf '  gh variable set AZURE_TENANT_ID       --body %s\n' "$TENANT"
printf '  gh variable set AZURE_SUBSCRIPTION_ID --body %s\n\n' "$SUBSCRIPTION"
printf 'And the workflow needs `permissions: id-token: write`, without which GitHub\n'
printf 'mints no token at all and azure/login fails on an empty assertion.\n'
