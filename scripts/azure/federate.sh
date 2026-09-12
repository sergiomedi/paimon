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

# ── Which subject this repository will actually present ─────────────────────
#
# Not the one you would write down. Since 15 July 2026 every new repository —
# and every repository renamed or transferred after that date — presents an
# **immutable** subject claim with the numeric ids of the owner and the
# repository embedded in it:
#
#   repo:sergiomedi@100800516/paimon@1353634150:ref:refs/heads/main
#
# rather than the familiar `repo:sergiomedi/paimon:ref:refs/heads/main`. Entra
# matches the subject exactly, so a credential written the old way matches
# nothing and the run fails with AADSTS700213 — which prints the subject it was
# presented, and is the only place that string appears.
#
# The reason for the change is worth knowing rather than working around: a name
# can be given up and taken by somebody else, and a credential trusting a name
# would follow it. An id cannot be re-registered.
#
# So the ids are read from GitHub rather than guessed.
OWNER="${REPOSITORY%%/*}"
NAME="${REPOSITORY##*/}"

bold "▸ resolving the repository"
METADATA="$(curl --silent --show-error --max-time 30 \
    "https://api.github.com/repos/${REPOSITORY}" || printf '')"
IDS="$(printf '%s' "$METADATA" | python3 -c '
import json
import sys

try:
    data = json.load(sys.stdin)
except json.JSONDecodeError:
    sys.exit(1)
if "id" not in data or "owner" not in data:
    sys.exit(1)
print(data["owner"]["id"], data["id"], data.get("created_at", ""))
' || printf '')"

[[ -n "$IDS" ]] || die "$(printf '%s\n' \
    "could not read https://api.github.com/repos/${REPOSITORY}." \
    "" \
    "The numeric ids of the owner and the repository are part of the subject Entra" \
    "has to trust, and guessing them is not an option. If the repository is private," \
    "fetch them with an authenticated call and set them by hand:" \
    "" \
    "  gh api repos/${REPOSITORY} --jq '.owner.id, .id'")"

read -r OWNER_ID REPOSITORY_ID CREATED <<<"$IDS"
printf '  owner %s (%s), repository %s (%s)\n' "$OWNER" "$OWNER_ID" "$NAME" "$REPOSITORY_ID"
printf '  created %s\n\n' "${CREATED:-unknown}"

IMMUTABLE="${OWNER}@${OWNER_ID}/${NAME}@${REPOSITORY_ID}"

#: The trigger this credential is for, and the subject GitHub will present.
#:
#: One per line, name and subject, because Entra matches the subject exactly and
#: a run whose subject is not listed gets no token at all. That exactness is the
#: security property: `pull_request` is deliberately absent, so a fork's pull
#: request cannot deploy anything, and `environment:production` is separate so
#: that promotion needs the approval GitHub attaches to that environment.
#:
#: Both spellings are federated. The immutable one is what a repository created
#: after July 2026 presents; the legacy one is what an older repository that has
#: not adopted the format still presents, and this script cannot tell which from
#: outside without guessing at a date. Two credentials naming one repository is
#: not a widening of who may deploy — but the legacy one does trust a *name*, so
#: the report at the end says how to remove it once a run has proved which is in
#: use.
CREDENTIALS=(
    "paimon-main:repo:${IMMUTABLE}:ref:refs/heads/main"
    "paimon-production:repo:${IMMUTABLE}:environment:production"
    "paimon-main-legacy:repo:${REPOSITORY}:ref:refs/heads/main"
    "paimon-production-legacy:repo:${REPOSITORY}:environment:production"
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

bold "▸ the pipeline's app role on the API"
# The last piece, and the one that is not obvious from anything the pipeline
# reports. Signing in to Azure is not the same as being allowed to call this
# platform's own API: the pipeline authenticates with *client credentials*, and
# Entra issues no token at all for a resource the calling application holds no
# app role on — it says the application is not assigned to a role, which sounds
# like an Azure RBAC problem and is not one.
#
# The delegated scope from Phase 7 does not help here. Pre-authorising the Azure
# CLI covers a person at a terminal; a service principal is not a person and
# there is no consent to inherit.
if [[ -z "${AZURE_PAIMON_API_AUDIENCE:-}" ]]; then
    warn "  AZURE_PAIMON_API_AUDIENCE is not set, so the API's registration is unknown and"
    warn "  the role cannot be assigned. The pipeline will deploy and its smoke test will"
    warn "  get a 401. Set it and run this again:"
    warn ""
    warn "    export AZURE_PAIMON_API_AUDIENCE=\"api://\$(az ad app list \\"
    warn "      --display-name paimon-api --query '[0].appId' -o tsv)\""
    printf '\n'
else
    API_APP_ID="$(api_app_id)"
    # Defined on the registration by app_registration.py, which token.sh runs.
    # Reading it from the service principal rather than the application because
    # that is the object the assignment is made against, and the two can
    # disagree for a minute after a change.
    ROLE_ID="$(az ad sp show --id "$API_APP_ID" \
        --query "appRoles[?value=='Deployment.Verify'].id | [0]" -o tsv 2>/dev/null || printf '')"
    API_SP_ID="$(az ad sp show --id "$API_APP_ID" --query id -o tsv 2>/dev/null || printf '')"

    if [[ -z "$ROLE_ID" || -z "$API_SP_ID" ]]; then
        warn "  the API's registration has no Deployment.Verify role yet. token.sh adds it:"
        warn ""
        warn "    ./scripts/azure/token.sh"
        warn ""
        warn "  then run this again. The role has to exist before it can be assigned."
        printf '\n'
    elif az rest --method GET \
        --url "https://graph.microsoft.com/v1.0/servicePrincipals/${API_SP_ID}/appRoleAssignedTo" \
        --query "value[?principalId=='${OBJECT_ID}'] | [0].id" -o tsv 2>/dev/null | grep -q .; then
        printf '  Deployment.Verify already assigned\n\n'
    else
        # principalId is who gets it, resourceId is who defined it, appRoleId is
        # which one. All three are object ids of service principals or roles, and
        # none of them is an application id — a mistake that returns a
        # "Request_BadRequest" naming none of the three.
        az rest --method POST \
            --url "https://graph.microsoft.com/v1.0/servicePrincipals/${API_SP_ID}/appRoleAssignedTo" \
            --headers 'Content-Type=application/json' \
            --body "$(printf '{"principalId": "%s", "resourceId": "%s", "appRoleId": "%s"}' \
                "$OBJECT_ID" "$API_SP_ID" "$ROLE_ID")" -o none
        printf '  Deployment.Verify assigned\n\n'
    fi
fi

bold "▸ one credential you can probably delete"
printf 'Four were federated: two for the immutable subject and two for the legacy one.\n'
printf 'A run only ever presents one of the two, and its log says which — look for\n'
printf '"subject claim" in the "Sign in to Azure" step. Delete the pair that was not\n'
printf 'used, because the legacy subject trusts a repository *name*, and a name can be\n'
printf 'given up and taken by somebody else:\n\n'
printf '  az ad app federated-credential delete --id %s \\\n' "$APP_ID"
printf '    --federated-credential-id paimon-main-legacy\n'
printf '  az ad app federated-credential delete --id %s \\\n' "$APP_ID"
printf '    --federated-credential-id paimon-production-legacy\n\n'

bold "▸ put these in the repository, as variables rather than secrets"
printf 'None of them is a credential: an application id, a tenant id and a subscription id\n'
printf 'are identifiers, and holding all three gets nobody a token. The token comes from\n'
printf 'GitHub, for a run whose subject Entra was told about above.\n\n'
printf '  gh variable set AZURE_CLIENT_ID       --body %s\n' "$APP_ID"
printf '  gh variable set AZURE_TENANT_ID       --body %s\n' "$TENANT"
printf '  gh variable set AZURE_SUBSCRIPTION_ID --body %s\n\n' "$SUBSCRIPTION"
printf 'And the workflow needs `permissions: id-token: write`, without which GitHub\n'
printf 'mints no token at all and azure/login fails on an empty assertion.\n'
