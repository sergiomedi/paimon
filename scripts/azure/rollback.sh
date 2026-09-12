#!/usr/bin/env bash
# Put the previous revision back in front of traffic.
#
#   ./scripts/azure/rollback.sh                 back to the previous revision
#   ./scripts/azure/rollback.sh <suffix>        back to a particular one
#
# Changes one weight. No build, no deployment, no image to find: the revision is
# still there, still running, and was serving every request a few minutes ago.
#
# This script is short because the decision that made it short was made
# elsewhere — multiple active revisions, in ADR-0044. Under a single revision the
# equivalent is "deploy the previous image again", which means finding which
# image that was, waiting for a build, and hoping the previous configuration was
# part of it. Under multiple revisions it is a weight.
#
# It deliberately does not check anything before acting. A rollback is run when
# something is already wrong, and a script that pauses to verify the version that
# was working ten minutes ago is a script that makes an outage longer.

source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

require_az
announce
load_outputs

APP="${AZURE_API_NAME:-ca-paimon-api-${ENVIRONMENT}}"
CURRENT="$(live_revision)"
TARGET="${1:-}"

if [[ -z "$TARGET" ]]; then
    # The most recent revision that is not the one serving. Ordered by creation
    # time rather than by name: revision suffixes here are commit hashes, and
    # those do not sort chronologically.
    TARGET="$(az containerapp revision list --name "$APP" --resource-group "$GROUP" \
        --query "sort_by([?properties.active && name != '${APP}--${CURRENT}'], &properties.createdTime) | [-1].name" \
        -o tsv 2>/dev/null || printf '')"
    TARGET="${TARGET#"${APP}"--}"
fi

[[ -n "$TARGET" ]] || die "$(printf '%s\n' \
    "no other active revision to go back to." \
    "" \
    "maxInactiveRevisions is 2, so this environment keeps the one serving and the one" \
    "before it. If both are gone, going back means deploying the image again:" \
    "  ./scripts/azure/release.sh <image>")"

bold "▸ rolling back"
printf '  from   %s\n' "${CURRENT:-nothing}"
printf '  to     %s\n\n' "$TARGET"

az containerapp ingress traffic set --name "$APP" --resource-group "$GROUP" \
    --revision-weight "${APP}--${TARGET}=100" -o none

# The labels follow the traffic, so that `blue` keeps meaning "what is serving"
# rather than "what was serving the last time somebody released". A label that
# lies is worse than no label: the next release would check the wrong hostname.
az containerapp revision label add --name "$APP" --resource-group "$GROUP" \
    --label blue --revision "${APP}--${TARGET}" -o none 2>/dev/null || true

bold "▸ rolled back"
printf 'Serving %s\n\n' "$TARGET"
printf 'The revision that was serving is still there and still running — nothing was\n'
printf 'deleted, so this is reversible in the same way and by the same command:\n'
printf '  ./scripts/azure/release.sh   or   ./scripts/azure/rollback.sh %s\n\n' "${CURRENT:-<suffix>}"
printf 'Write down what went wrong before the environment or the revision ages out.\n'
printf 'maxInactiveRevisions is 2: the third release from now removes this one.\n'
